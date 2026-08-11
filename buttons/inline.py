"""
All inline keyboards for Aura Music Bot.

Callback data convention:  <namespace>:<action>[:payload]
Namespaces: main, pl (player), vol, q (queue), adm (admin),
            grp (groups), ow (owner), set (settings), cf (confirm)
"""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _row(*buttons: InlineKeyboardButton) -> list:
    return list(buttons)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


# --------------------------------------------------------------------------
# Main menu
# --------------------------------------------------------------------------
def main_menu(is_admin: bool = False, is_owner: bool = False) -> InlineKeyboardMarkup:
    # NOTE (2026-08-11, user-mandated): NO admin/owner buttons in the main
    # menu — "buttons are too much looks weird". Admin tools are command-only
    # (/admin, /cookies, etc.).
    kb = [
        _row(_btn("🎵 Play Music", "main:music"), _btn("🎬 Play Video", "main:video")),
        _row(_btn("📜 Help", "main:help"), _btn("⚙️ Settings", "main:settings")),
        _row(_btn("📊 Bot Stats", "main:stats"), _btn("💾 Saved", "main:saved"), _btn("👨‍💻 Developer", "main:dev")),
    ]
    kb.append(_row(_btn("❌ Close", "main:close")))
    return InlineKeyboardMarkup(kb)


def close_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([_row(_btn("❌ Close", "main:close"))])


def back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [_row(_btn("⬅️ Back", "main:back"), _btn("❌ Close", "main:close"))]
    )


# --------------------------------------------------------------------------
# Player controls
# --------------------------------------------------------------------------
def player_kb(state: dict) -> InlineKeyboardMarkup:
    """state: {'paused': bool, 'loop': bool, 'volume': int, 'has_queue': bool}"""
    pause_btn = _btn("▶️ Resume", "pl:resume") if state.get("paused") else _btn("⏸️ Pause", "pl:pause")
    loop_txt = "🔁 Loop: ON" if state.get("loop") else "🔁 Loop"
    kb = [
        _row(pause_btn, _btn("⏭️ Skip", "pl:skip")),
        _row(_btn("⏪ -10s", "pl:seekb"), _btn("⏩ +10s", "pl:seekf")),
        _row(_btn("⏹️ Stop", "pl:stop"), _btn("🔊 Volume", "pl:vol"), _btn("📴 Leave VC", "pl:leave")),
        _row(_btn("📜 Queue", "pl:queue"), _btn(loop_txt, "pl:loop")),
        _row(_btn("💾 Saved", "main:saved")),
    ]
    if state.get("has_queue"):
        kb.append(_row(_btn("⏯️ Next ▶️", "pl:next")))
    kb.append(_row(_btn("❌ Close Player", "pl:close")))
    return InlineKeyboardMarkup(kb)


def volume_kb(volume: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _row(_btn("🔉 -10", "vol:-10"), _btn(f"🔊 {volume}", "vol:nop"), _btn("🔊 +10", "vol:+10")),
            _row(_btn("🔇 Mute", "vol:mute"), _btn("🔈 Unmute", "vol:unmute")),
            _row(_btn("⬅️ Back", "vol:back")),
        ]
    )


# --------------------------------------------------------------------------
# Queue menu (pagination)
# --------------------------------------------------------------------------
def queue_kb(
    page: int,
    total_pages: int,
    is_admin: bool = False,
    items: list = None,
) -> InlineKeyboardMarkup:
    """items: [(queue_index, title)] for the current page — renders a ❌ remove button per track (admins only)."""
    kb = []
    if is_admin and items:
        for idx, title in items:
            kb.append(_row(_btn(f"❌ #{idx} {_short(title, 30)}", f"q:rm:{idx}")))
    nav = []
    if page > 1:
        nav.append(_btn("⬅️ Prev", f"q:pg:{page - 1}"))
    nav.append(_btn(f"{page}/{total_pages}", "q:nop"))
    if page < total_pages:
        nav.append(_btn("Next ➡️", f"q:pg:{page + 1}"))
    kb.append(_row(*nav))
    if is_admin:
        kb.append(_row(_btn("🗑️ Clear Queue", "q:clear")))
    kb.append(_row(_btn("❌ Close", "q:close")))
    return InlineKeyboardMarkup(kb)


# --------------------------------------------------------------------------
# Saved library (auto history of every played track)
# --------------------------------------------------------------------------
def _short(s: str, n: int = 26) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def saved_hint_kb() -> InlineKeyboardMarkup:
    """Quick access to the Saved library (shown wherever a 'how to play' hint appears)."""
    return InlineKeyboardMarkup(
        [
            _row(_btn("💾 Saved Library", "main:saved")),
            _row(_btn("❌ Close", "main:close")),
        ]
    )


def saved_kb(items, page: int, total_pages: int, is_owner: bool = False) -> InlineKeyboardMarkup:
    kb = []
    for it in items:
        icon = "🎬" if it["is_video"] else "🎵"
        row = [_btn(f"{icon} {_short(it['title'])}", f"sv:play:{it['id']}")]
        # owner-only delete button on each saved track
        if is_owner:
            row.append(_btn("🗑️", f"sv:del:{it['id']}:{page}"))
        kb.append(_row(*row))
    nav = []
    if page > 1:
        nav.append(_btn("⬅️ Prev", f"sv:pg:{page - 1}"))
    nav.append(_btn(f"{page}/{total_pages}", "sv:nop"))
    if page < total_pages:
        nav.append(_btn("Next ➡️", f"sv:pg:{page + 1}"))
    kb.append(_row(*nav))
    kb.append(_row(_btn("⬅️ Back", "main:back"), _btn("❌ Close", "main:close")))
    return InlineKeyboardMarkup(kb)


# --------------------------------------------------------------------------
# Admin panel
# --------------------------------------------------------------------------
def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _row(_btn("➕ Add Admin", "adm:add"), _btn("➖ Remove Admin", "adm:rm")),
            _row(_btn("👥 Admin List", "adm:list"), _btn("🚫 Ban User", "adm:ban")),
            _row(_btn("✅ Unban User", "adm:unban"), _btn("📢 Broadcast", "adm:bcast")),
            _row(_btn("📈 Statistics", "adm:stats"), _btn("👑 Owner Panel", "adm:owner")),
            _row(_btn("👥 Group Management", "adm:groups")),
            _row(_btn("🔄 Restart Bot", "adm:restart"), _btn("🛑 Shutdown Bot", "adm:shutdown")),
            _row(_btn("⬅️ Back", "main:back")),
        ]
    )


def confirm_kb(action: str, target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _row(
                _btn("✅ Yes", f"cf:yes:{action}:{target}"),
                _btn("❌ No", f"cf:no:{action}:{target}"),
            )
        ]
    )


def broadcast_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _row(
                _btn("👥 Users", "bcast:users"),
                _btn("👥 Groups", "bcast:groups"),
                _btn("🌐 All", "bcast:all"),
            ),
            _row(_btn("⬅️ Back", "adm:back")),
        ]
    )


# --------------------------------------------------------------------------
# Group management
# --------------------------------------------------------------------------
def group_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _row(_btn("🚪 Leave Group", "grp:leave"), _btn("🚫 Blacklist Group", "grp:bl")),
            _row(_btn("✅ Whitelist Group", "grp:wl"), _btn("📋 Group List", "grp:list")),
            _row(_btn("⚙️ Group Settings", "grp:set")),
            _row(_btn("⬅️ Back", "adm:back")),
        ]
    )


def group_setting_kb(chat_id: int, streaming: bool) -> InlineKeyboardMarkup:
    state = "🟢 Enabled" if streaming else "🔴 Disabled"
    return InlineKeyboardMarkup(
        [
            _row(_btn(f"🎧 Streaming: {state}", f"grp:set:{chat_id}:toggle")),
            _row(_btn("⬅️ Back", "grp:back")),
        ]
    )


# --------------------------------------------------------------------------
# Owner panel
# --------------------------------------------------------------------------
def owner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            _row(_btn("👥 All Users", "ow:users"), _btn("👥 All Groups", "ow:groups")),
            _row(_btn("🎧 Active VCs", "ow:active"), _btn("📈 System Info", "ow:sys")),
            _row(_btn("🚫 Ban User", "ow:ban"), _btn("✅ Unban User", "ow:unban")),
            _row(_btn("🚫 Ban Group", "ow:ban_group"), _btn("✅ Unban Group", "ow:unban_group")),
            _row(_btn("👥 Admins", "ow:admins"), _btn("📜 Logs", "ow:logs")),
            _row(_btn("🔄 Update Bot", "ow:update"), _btn("🛑 Shutdown", "ow:shutdown")),
            _row(_btn("⬅️ Back", "adm:back")),
        ]
    )


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def settings_menu(autodel: bool, prefix: str = "") -> InlineKeyboardMarkup:
    kb = [
        _row(_btn(f"🗑️ Auto-delete: {'ON' if autodel else 'OFF'}", "set:autodel")),
        _row(_btn("⬅️ Back", "main:back")),
    ]
    return InlineKeyboardMarkup(kb)


# --------------------------------------------------------------------------
# Song requests (member → admin approval)
# --------------------------------------------------------------------------
def owner_secret_kb(offline: bool = False) -> InlineKeyboardMarkup:
    """Owner-only secret panel buttons (🔒 in the main menu)."""
    action = "🛑 Shutdown" if not offline else "💥 KABOOM!"
    data = "owsec:shutdown" if not offline else "owsec:kaboom"
    return InlineKeyboardMarkup(
        [
            _row(_btn(action, data)),
            _row(_btn("🔒 Secret Commands", "owsec:panel")),
            _row(_btn("⬅️ Back", "main:back"), _btn("❌ Close", "main:close")),
        ]
    )


def request_status_kb(req_id: int) -> InlineKeyboardMarkup:
    """Shown to the requester after /request — not actionable, just status."""
    return InlineKeyboardMarkup(
        [
            _row(_btn("📋 My Request", f"req:status:{req_id}")),
            _row(_btn("❌ Close", "main:close")),
        ]
    )


def request_approve_kb(req_id: int) -> InlineKeyboardMarkup:
    """Admin approval panel for ONE pending song request (used on the
    single-request notice posted when someone requests)."""
    return InlineKeyboardMarkup(
        [
            _row(
                _btn("✅ Approve", f"req:approve:{req_id}"),
                _btn("❌ Reject", f"req:reject:{req_id}"),
            ),
            _row(_btn("📋 All Pending", f"req:list")),
            _row(_btn("❌ Close", "main:close")),
        ]
    )


def request_list_kb(pending: list) -> InlineKeyboardMarkup:
    """Admin panel for the full pending list — every request gets its OWN
    Approve/Reject row carrying the real request id."""
    rows = []
    for r in pending:
        rows.append(
            _row(
                _btn(
                    f"✅ #{r['id']} {truncate_label(r['query'], 18)}",
                    f"req:approve:{r['id']}",
                ),
                _btn("❌", f"req:reject:{r['id']}"),
            )
        )
    rows.append(_row(_btn("🔄 Refresh", "req:list")))
    rows.append(_row(_btn("❌ Close", "main:close")))
    return InlineKeyboardMarkup(rows)


def truncate_label(s: str, n: int) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"
