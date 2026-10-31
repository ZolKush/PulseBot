"""Copy an existing project into a private compose-and-data deployment bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .deployment_config import DeploymentPreparationError, docker_configuration, load_object, write_object

_IMAGE_PLACEHOLDER = "${MAINTBOT_IMAGE:-maintbot:latest}"
_IMAGE_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE_DOMAIN_COMPONENT = r"(?:[A-Za-z0-9]|[A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9])"
_IMAGE_REGISTRY = rf"(?:{_IMAGE_DOMAIN_COMPONENT}(?:\.{_IMAGE_DOMAIN_COMPONENT})*|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?"
_IMAGE_REFERENCE = re.compile(
    rf"(?P<name>(?:{_IMAGE_REGISTRY}/)?{_IMAGE_PATH_COMPONENT}(?:/{_IMAGE_PATH_COMPONENT})*)"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"(?:@sha256:[0-9a-f]{64})?"
)


@dataclass(frozen=True)
class DeploymentReport:
    output_dir: str
    source_format: str
    server_count: int
    omitted_host_servers: int
    telegram_state_present: bool
    original_config_backup: str


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _regular_tree(root: Path) -> list[Path]:
    if not root.is_dir() or _is_link(root):
        raise DeploymentPreparationError("a real source directory is required")
    files: list[Path] = []
    for path in root.rglob("*"):
        if _is_link(path) or not (path.is_dir() or path.is_file()):
            raise DeploymentPreparationError("source contains symbolic links or non-regular files")
        if path.is_file():
            files.append(path)
    return files


def _digest(path: Path) -> bytes:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while part := stream.read(1024 * 1024):
            digest.update(part)
    return digest.digest()


def _source_format(data_dir: Path) -> str:
    if (data_dir / "maintbot.sqlite3").is_file():
        return "sqlite-v1"
    if (data_dir / "storage_layout.json").is_file():
        return "split-json-v1"
    raise DeploymentPreparationError("source data requires SQLite or split JSON layout with storage_layout.json")


def _compose_document(project: Path, image: str | None) -> str:
    if image is not None:
        reference = _IMAGE_REFERENCE.fullmatch(image) if len(image) <= 512 else None
        if reference is None or len(reference.group("name")) > 255:
            raise DeploymentPreparationError("image must be a valid Docker image reference")
    template = project / "deploy" / ("compose.image.yaml" if image is not None else "compose.remote.yaml")
    if not template.is_file():
        raise DeploymentPreparationError("deployment Compose template is missing")
    try:
        document = template.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise DeploymentPreparationError("deployment Compose template must be valid UTF-8") from exc
    if image is not None:
        if document.count(_IMAGE_PLACEHOLDER) != 1:
            raise DeploymentPreparationError("image deployment Compose template has an invalid image placeholder")
        document = document.replace(_IMAGE_PLACEHOLDER, json.dumps(image))
    return document


def _selected_config(project: Path, selected: Path | None) -> Path:
    if selected is not None:
        return project / selected
    candidates = [path for path in (project / "data/conf", project / "conf") if (path / "bot.json").is_file()]
    if len(candidates) != 1:
        raise DeploymentPreparationError("select one existing operator configuration directory with --config-dir")
    return candidates[0]


def _configured_pickle(project: Path, config_dir: Path) -> Path | None:
    bot = load_object(config_dir / "bot.json", description="bot configuration")
    raw_path = bot.get("PTB_PERSISTENCE_PATH", "")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    direct = project / raw_path
    if direct.is_file() and project in direct.resolve().parents:
        return direct
    # Paths in the copied config may still refer to the removed Linux server.
    old_data = PurePosixPath(str(bot.get("DATA_DIR", "")).replace("\\", "/"))
    old_pickle = PurePosixPath(raw_path.replace("\\", "/"))
    for previous_root, current_root in ((old_data, project / "data"), (old_data.parent, project)):
        try:
            relative = old_pickle.relative_to(previous_root)
        except ValueError:
            continue
        candidate = current_root.joinpath(*relative.parts)
        if candidate.is_file() and project in candidate.resolve().parents:
            return candidate
    return None


def _select_pickle(project: Path, config_dir: Path, selected: Path | None) -> Path | None:
    if selected is not None:
        path = project / selected
        if not path.is_file() or _is_link(path):
            raise DeploymentPreparationError("--ptb-pickle must name an existing trusted regular file")
        return path
    configured = _configured_pickle(project, config_dir)
    if configured is not None:
        return configured
    data = project / "data"
    primaries = [path for path in (data / "telegram/persistence.pickle", data / "ptb_persistence") if path.is_file()]
    backups = [
        path for path in (data / "telegram/persistence.pickle.bak", data / "ptb_persistence.bak") if path.is_file()
    ]
    candidates = primaries or backups
    if len(candidates) > 1:
        raise DeploymentPreparationError("select the actual Telegram state file with --ptb-pickle")
    if candidates:
        return candidates[0]
    bot = load_object(config_dir / "bot.json", description="bot configuration")
    if bot.get("PTB_PERSISTENCE_PATH"):
        raise DeploymentPreparationError("configured Telegram state is absent; locate it and pass --ptb-pickle")
    return None


def prepare_deployment(
    project_dir: Path,
    output_dir: Path,
    *,
    panel_only: bool = False,
    config_dir: Path | None = None,
    secrets_file: Path | None = None,
    ptb_pickle: Path | None = None,
    image: str | None = None,
) -> DeploymentReport:
    """Prepare a new bundle; original data, settings and secrets stay untouched."""
    if any(_is_link(path) for path in (output_dir, *output_dir.absolute().parents)):
        raise DeploymentPreparationError("output directory must not use symbolic links")
    if _is_link(project_dir):
        raise DeploymentPreparationError("source project directory must not use symbolic links")
    project = project_dir.resolve()
    output = output_dir.resolve()
    data = project / "data"
    if output.exists() or output == project or data == output or data in output.parents:
        raise DeploymentPreparationError("refusing existing output or output inside source data")
    source_files = _regular_tree(data)
    source_format = _source_format(data)
    config = _selected_config(project, config_dir)
    _regular_tree(config)
    secret = project / (secrets_file or Path("app/env.secrets"))
    if not secret.is_file() or _is_link(secret) or secret.stat().st_size == 0:
        raise DeploymentPreparationError("a nonempty existing secrets file is required")
    compose = _compose_document(project, image)
    bot, servers, omitted = docker_configuration(
        config, template=project / "examples/conf/docker-bot.json", panel_only=panel_only
    )
    pickle_path = _select_pickle(project, config, ptb_pickle) if source_format != "sqlite-v1" else None
    checksums = {path.relative_to(data): _digest(path) for path in source_files}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.parent / f".{output.name}.prepare-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    try:
        copied = temporary / "data"
        shutil.copytree(data, copied)
        if {path.relative_to(data) for path in _regular_tree(data)} != set(checksums):
            raise DeploymentPreparationError("source data changed while preparing deployment")
        if any(
            _digest(data / name) != digest or _digest(copied / name) != digest for name, digest in checksums.items()
        ):
            raise DeploymentPreparationError("source data changed or copy verification failed")
        backup = copied / "config-before-docker"
        if backup.exists():
            backup = copied / f"config-before-docker-{uuid.uuid4().hex}"
        shutil.copytree(config, backup)
        target_config = copied / "conf"
        if target_config.exists():
            shutil.rmtree(target_config)
        target_config.mkdir(mode=0o700)
        server_dir = target_config / "servers"
        server_dir.mkdir(mode=0o700)
        write_object(target_config / "bot.json", bot)
        for name, document in servers.items():
            write_object(server_dir / name, document)
        shutil.copyfile(secret, copied / "env.secrets")
        (copied / "env.secrets").chmod(0o600)
        if pickle_path is not None:
            shutil.copyfile(pickle_path, copied / "telegram-migration.pickle")
            (copied / "telegram-migration.pickle").chmod(0o600)
        (temporary / "compose.yaml").write_text(compose, encoding="utf-8", newline="\n")
        if output.exists():
            raise DeploymentPreparationError("output appeared while preparing deployment")
        os.replace(temporary, output)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return DeploymentReport(str(output), source_format, len(servers), omitted, pickle_path is not None, backup.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("release"))
    parser.add_argument("--panel-only", action="store_true")
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--secrets-file", type=Path)
    parser.add_argument("--ptb-pickle", type=Path)
    parser.add_argument("--image", help="Use this locally loaded Docker image instead of a remote source build.")
    args = parser.parse_args()
    try:
        report = prepare_deployment(
            args.project_dir,
            args.output,
            panel_only=args.panel_only,
            config_dir=args.config_dir,
            secrets_file=args.secrets_file,
            ptb_pickle=args.ptb_pickle,
            image=args.image,
        )
    except DeploymentPreparationError as exc:
        print(f"Deployment preparation refused: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("Deployment preparation failed; check source paths and file permissions.", file=sys.stderr)
        return 2
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
