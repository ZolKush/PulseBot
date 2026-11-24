"use strict";

let telegram;
const session = new URLSearchParams(location.search).get("session");
const widget = document.querySelector("#widget");
const statusText = document.querySelector("#status");
const retry = document.querySelector("#retry");
const unavailable = "Не удалось подключиться к Cloudflare. Проверьте соединение или попробуйте другую сеть.";
let submitting = false;

function show(message, canRetry = false) {
  statusText.textContent = message;
  retry.hidden = !canRetry;
}

function returnToBot(message) {
  show(message, true);
  retry.textContent = "Вернуться в бот";
  retry.onclick = () => telegram.close();
}

function applyTheme() {
  document.documentElement.dataset.theme = telegram?.colorScheme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

retry.onclick = () => location.reload();
applyTheme();

async function submit(token) {
  if (submitting) return;
  submitting = true;
  show("Проверяем результат…");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 25000);
  try {
    const response = await fetch("/verify", {
      method: "POST",
      credentials: "omit",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({initData: telegram.initData, session, token}),
      signal: controller.signal
    });
    if (!response.ok) {
      const messages = {
        403: "Время входа истекло. Закройте окно и откройте проверку из бота.",
        409: "Отправьте /start в боте. Если попытки закончились, новая проверка станет доступна в течение 10 минут.",
        422: "Проверка устарела или не принята. Попробуйте ещё раз.",
        429: "Слишком много запросов. Подождите немного и попробуйте ещё раз.",
        503: "Сервис проверки временно недоступен. Попробуйте позже."
      };
      if ([403, 409].includes(response.status)) {
        returnToBot(messages[response.status]);
      } else {
        show(messages[response.status] || "Не удалось завершить проверку.", true);
      }
      return;
    }
    const result = await response.json();
    if (result.continued === false) {
      returnToBot("Проверка пройдена. Отправьте /start в боте, чтобы продолжить регистрацию.");
      return;
    }
    if (result.continued !== true) throw new Error("Unexpected verification response");
    show("Проверка пройдена. Возвращаемся в бот…");
    setTimeout(() => telegram.close(), 900);
  } catch {
    show("Не удалось получить ответ. Откройте бот и отправьте /start, чтобы проверить результат.", true);
  } finally {
    clearTimeout(timer);
  }
}

async function start() {
  telegram = window.Telegram?.WebApp;
  applyTheme();
  telegram?.onEvent("themeChanged", applyTheme);
  telegram?.ready();
  show("Подключаем проверку…");
  if (!telegram?.initData || !session) {
    show("Откройте проверку кнопкой в личном чате с ботом ZeroNet.");
    return;
  }
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(session));
  const cData = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
  const timer = setTimeout(() => show(unavailable, true), 12000);
  window.onTurnstileReady = () => {
    clearTimeout(timer);
    show("Следуйте инструкциям в окне проверки.");
    try {
      window.turnstile.render(widget, {
        sitekey: widget.dataset.sitekey,
        action: "registration",
        cData,
        theme: document.documentElement.dataset.theme,
        size: widget.clientWidth >= 300 ? "flexible" : "compact",
        language: "ru",
        retry: "never",
        callback: submit,
        "error-callback": () => { if (!submitting) show(unavailable, true); },
        "expired-callback": () => { if (!submitting) show("Время проверки истекло.", true); },
        "timeout-callback": () => { if (!submitting) show("Проверка не завершена вовремя.", true); }
      });
    } catch {
      show(unavailable, true);
    }
  };
  const script = document.createElement("script");
  script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onTurnstileReady";
  script.onerror = () => { clearTimeout(timer); show(unavailable, true); };
  document.head.append(script);
}
function openMiniApp() {
  start().catch(() => show("Не удалось открыть проверку. Обновите Telegram и повторите попытку.", true));
}

if (!session || window.Telegram?.WebApp) {
  openMiniApp();
} else {
  const sdk = document.querySelector("#telegram-sdk");
  const failed = () => show("Не удалось подключиться к Telegram. Проверьте соединение и попробуйте снова.", true);
  const timer = setTimeout(failed, 12000);
  sdk.addEventListener("load", () => { clearTimeout(timer); openMiniApp(); }, {once: true});
  sdk.addEventListener("error", () => { clearTimeout(timer); failed(); }, {once: true});
}
