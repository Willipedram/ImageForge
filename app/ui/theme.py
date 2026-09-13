"""Professional light/dark Windows theme system."""

from __future__ import annotations


def stylesheet(theme: str = "system") -> str:
    dark = theme == "dark"
    colors = {
        "window": "#0b1120" if dark else "#f4f7fb", "panel": "#151e30" if dark else "#ffffff",
        "text": "#e8eef8" if dark else "#172033", "muted": "#93a2b8" if dark else "#65748b",
        "border": "#26344d" if dark else "#dfe6f0", "input": "#111a2b" if dark else "#ffffff",
        "sidebar": "#0a1020" if dark else "#111c32", "hover": "#1d2b46", "accent": "#3978f6",
    }
    return f'''QMainWindow, QWidget {{ background: {colors["window"]}; color: {colors["text"]};
        font-family: "Segoe UI Variable", "Segoe UI"; font-size: 10pt; }}
    #sidebar {{ background: {colors["sidebar"]}; border-right: 1px solid #24304a; }}
    #brand {{ color: #fff; font-size: 18pt; font-weight: 700; letter-spacing: 2px; }}
    #tagline, #versionLabel, #mutedLabel, #pageSubtitle {{ color: {colors["muted"]}; }}
    #navigation {{ background: transparent; border: 0; color: #b8c4d8; outline: none; margin-top: 20px; }}
    #navigation::item {{ padding: 11px 12px; border-radius: 7px; margin: 2px 0; }}
    #navigation::item:selected {{ background: {colors["accent"]}; color: white; }}
    #navigation::item:hover:!selected {{ background: {colors["hover"]}; color: white; }}
    #pageHeading {{ font-size: 23pt; font-weight: 650; color: {colors["text"]}; }}
    #heroCard, #metricCard, #panelCard, #settingsPanel, #currentJob {{ background: {colors["panel"]};
        border: 1px solid {colors["border"]}; border-radius: 10px; }}
    #heroCard, #currentJob {{ padding: 10px; }} #currentJobTitle {{ font-size: 16pt; font-weight: 650; }}
    #metricCard {{ min-height: 76px; padding: 6px; }} #metricTitle {{ color: {colors["muted"]}; font-size: 8pt; font-weight: 650; }}
    #metricValue {{ color: {colors["text"]}; font-size: 15pt; font-weight: 650; }}
    #stageEstimate {{ color: {colors["muted"]}; padding: 8px; }}
    QProgressBar {{ height: 12px; border: 0; border-radius: 6px; background: {colors["border"]}; color: transparent; }}
    QProgressBar::chunk {{ background: {colors["accent"]}; border-radius: 6px; }}
    QComboBox, QSpinBox, QLineEdit, QTextEdit, QTableWidget, QListWidget {{ padding: 7px; border: 1px solid {colors["border"]};
        border-radius: 6px; background: {colors["input"]}; color: {colors["text"]}; selection-background-color: {colors["accent"]}; }}
    QHeaderView::section {{ background: {colors["panel"]}; color: {colors["muted"]}; padding: 8px; border: 0;
        border-bottom: 1px solid {colors["border"]}; font-weight: 600; }}
    QPushButton {{ padding: 8px 15px; border: 1px solid {colors["border"]}; border-radius: 6px; background: {colors["panel"]}; }}
    QPushButton:hover {{ border-color: {colors["accent"]}; }} QPushButton:disabled {{ color: {colors["muted"]}; }}
    #primaryButton {{ background: {colors["accent"]}; color: white; border: 0; font-weight: 600; }}
    #logViewer {{ font-family: "Cascadia Mono", Consolas; font-size: 9pt; }}
    QStatusBar {{ background: {colors["panel"]}; color: {colors["muted"]}; border-top: 1px solid {colors["border"]}; }}'''


STYLESHEET = stylesheet("light")
