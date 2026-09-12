"""Compact Windows-friendly visual theme."""

STYLESHEET = """
QMainWindow, QWidget { background: #f3f6fa; color: #172033; font-family: "Segoe UI"; font-size: 10pt; }
#sidebar { background: #111c32; }
#brand { color: #ffffff; font-size: 17pt; font-weight: 700; letter-spacing: 2px; }
#tagline, #versionLabel { color: #8390a8; font-size: 9pt; }
#navigation { background: transparent; border: 0; color: #b9c3d5; outline: none; margin-top: 24px; }
#navigation::item { padding: 12px 10px; border-radius: 6px; margin: 2px 0; }
#navigation::item:selected { background: #2563eb; color: white; }
#navigation::item:hover:!selected { background: #1c2942; color: white; }
#pageHeading { font-size: 22pt; font-weight: 650; color: #111827; }
#pageSubtitle { color: #687387; margin-bottom: 8px; }
#currentJob, #metricCard, #settingsPanel { background: white; border: 1px solid #e1e7f0; border-radius: 9px; }
#currentJob { padding: 8px; }
#currentJobTitle { font-size: 15pt; font-weight: 600; }
#metricCard { min-height: 82px; padding: 5px; }
#metricTitle { color: #718096; font-size: 8pt; font-weight: 600; }
#metricValue { color: #172033; font-size: 17pt; font-weight: 650; }
QProgressBar { height: 9px; border: 0; border-radius: 4px; background: #e7edf5; color: transparent; }
QProgressBar::chunk { background: #2563eb; border-radius: 4px; }
QComboBox, QSpinBox { padding: 7px; border: 1px solid #ccd5e3; border-radius: 5px; background: white; min-width: 180px; }
#primaryButton { background: #2563eb; color: white; padding: 8px 16px; border: 0; border-radius: 5px; font-weight: 600; }
QStatusBar { background: white; color: #536176; border-top: 1px solid #e1e7f0; }
"""
