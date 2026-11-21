"""Expected persistent-conversation routes used by routing contract tests."""

CONVERSATION_ROUTES = {
    "administration_flow": {
        "states": (81, 82),
        "entry_points": (
            r"callback:^(administration:input:(alias|help|support_email|payment_message|standard_price|"
            r"period_current|period_next)|staff:alias|"
            r"product:input:setting_(payment|current|next))$",
        ),
        "state_routes": {
            81: ("message",),
            82: (r"callback:^administration:confirm$",),
        },
        "fallbacks": (
            "command:cancel",
            r"callback:^administration:cancel$",
            r"callback:^menu:home$",
        ),
    },
    "profile_flow": {
        "states": (91,),
        "entry_points": (r"callback:^profile:email:edit$",),
        "state_routes": {91: ("message",)},
        "fallbacks": (
            "command:cancel",
            r"callback:^profile:show$",
            r"callback:^menu:home$",
        ),
    },
    "product_flow": {
        "states": (0, 1),
        "entry_points": (
            r"callback:^subscription:trial$",
            r"callback:^product:input:terms:\d+$",
            r"callback:^product:req:(approve|approve24|custom|reject|requisites|confirm|notfound):\d+$",
            r"callback:^product:input:(massdate|massremind|user_end:\d+|manualpay:\d+)$",
        ),
        "state_routes": {
            0: ("message",),
            1: (r"callback:^product:confirm:apply$",),
        },
        "fallbacks": (
            "command:cancel",
            r"callback:^(product:cancel|menu:home)$",
        ),
    },
    "maint_flow": {
        "states": (0, 1, 2, 3, 4, 5, 6),
        "entry_points": (
            "command:maint",
            r"callback:^menu:maint$",
            r"callback:^maint:extend:[0-9a-f]+$",
        ),
        "state_routes": {
            0: (r"callback:^maint:mode:(announce|schedule)$",),
            1: (r"callback:^maint:scope:(?:all|z[1-9][0-9]{0,19})$", r"callback:^maint:scope:page:[0-9]{1,9}$"),
            2: (r"callback:^maint:urgency:(urgent|planned)$",),
            3: ("message",),
            4: ("message",),
            5: ("message",),
            6: (
                r"callback:^maint:cal:nav:\d{4}-\d{2}$",
                r"callback:^maint:cal:day:\d{4}-\d{2}-\d{2}$",
                r"callback:^maint:cal:noop$",
            ),
        },
        "fallbacks": (
            "command:cancel",
            r"callback:^menu:home$",
        ),
    },
    "ticket_flow": {
        "states": (0, 1, 2, 3, 4, 5),
        "entry_points": (
            "command:ticket",
            r"callback:^menu:ticket$",
            r"callback:^ticket:adminreply:\d+$",
            r"callback:^ticket:userreply:\d+$",
            r"callback:^ticket:list(?::\d+)?$",
            r"callback:^ticket:open:\d+$",
            r"callback:^ticket:archive$",
            r"callback:^ticket:archive_page:\d+$",
            r"callback:^ticket:transfer_init:\d+$",
            r"callback:^ticket:transfer_to:\d+:\d+$",
        ),
        "state_routes": {
            0: ("message",),
            1: (r"callback:^ticket:(p1|p2|p3)$",),
            2: ("message",),
            3: (r"callback:^ticket:(send|edit_subj|edit_text|cancel)$",),
            4: ("message",),
            5: ("message",),
        },
        "fallbacks": (
            "command:cancel",
            r"callback:^menu:home$",
            r"callback:^ticket:list(?::\d+)?$",
            r"callback:^ticket:open:\d+$",
            r"callback:^ticket:archive$",
            r"callback:^ticket:archive_page:\d+$",
            r"callback:^ticket:transfer_init:\d+$",
            r"callback:^ticket:transfer_to:\d+:\d+$",
        ),
    },
    "users_flow": {
        "states": (0, 1, 2, 3, 4, 5, 6, 7),
        "entry_points": (
            "command:users",
            r"callback:^menu:users$",
            r"callback:^users:user:\d+$",
        ),
        "state_routes": {
            0: (
                r"callback:^users:(all|main|back|filter:(all|active|disabled|unpaid|admins|blocked)|"
                r"user:\d+|page:\d+)$",
            ),
            1: (r"callback:^users:(allmsg:(all|admins)|back)$",),
            2: ("message",),
            3: (r"callback:^users:(allsend|all|back)$",),
            4: (
                r"callback:^users:(msg:\d+|nick:\d+|cfg:\d+|subassign:\d+|subsend:\d+|subview:\d+|"
                r"toggle:\d+|toggleapply:\d+|access:(approve|block):\d+|"
                r"accessapply:(approve|block):\d+|back)$",
                r"callback:^users:user:\d+$",
            ),
            5: ("message",),
            6: ("message",),
            7: ("message", r"callback:^users:user:\d+$"),
        },
        "fallbacks": (
            "command:cancel",
            r"callback:^menu:home$",
        ),
    },
}

__all__ = ["CONVERSATION_ROUTES"]
