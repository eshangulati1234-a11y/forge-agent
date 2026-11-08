"""Shared colours and layout for Forge's terminal interface."""
from textual.theme import Theme


FORGE_BLACK = Theme(
    name="forge-black", primary="#ffffff", secondary="#a0a0a0", accent="#72c7e8",
    foreground="#ffffff", background="#000000", surface="#000000", panel="#000000",
    boost="#202020", success="#80c991", warning="#e4be71", error="#ef8585",
    dark=True, text_alpha=1.0, luminosity_spread=0,
    variables={"text-muted": "#a0a0a0", "border": "#444444", "footer-background": "#000000"},
)

CSS = """
Screen { background: $background; color: $foreground; }
#status { height: 3; padding: 0 2; }
TabbedContent { height: 1fr; }
TabbedContent > ContentTabs { display: none; }
TabPane { padding: 0 2; }
#chatlog { height: 1fr; }
#conversation { height: 1fr; layers: messages controls; }
#chatlog { layer: messages; }
#scroll-controls { layer: controls; dock: bottom; height: 3; align: center middle; display: none; }
#scroll-bottom { width: 14; min-width: 14; height: 1; padding: 0 1; border: none; background: $boost; }
#scroll-bottom:hover { background: $secondary; color: $background; }
#activity-indicator { height: 1; margin: 0 0 1 0; text-overflow: ellipsis; }
#input-row { height: auto; border-top: solid $secondary; border-bottom: solid $secondary; padding: 1 0; }
#prompt { width: 2; height: 1; }
#composer { height: 1; min-height: 1; max-height: 6; width: 1fr; border: none; padding: 0; background: $background; }
#composer:focus { border: none; }
#suggest { height: auto; max-height: 10; border: none; background: $background; display: none; }
#hints { height: 1; padding: 0 2; color: $text-muted; text-overflow: ellipsis; }
.detail { height: 1fr; border: none; padding: 0 1; }
.compact .management-split { layout: vertical; }
DataTable { height: 1fr; background: $background; }
DataTable > .datatable--header { background: $boost; color: $foreground; text-style: bold; }
DataTable > .datatable--cursor { background: $boost; color: $foreground; }
OptionList { background: $background; border: none; }
OptionList > .option-list--option-highlighted { background: $boost; color: $foreground; text-style: bold; }
Input { background: $background; border: solid $secondary; }
Input:focus { border: solid $foreground; }
Button { background: $boost; color: $foreground; border: none; }
RichLog { background: $background; }
"""
