"""Professional dashboard with incremental, bounded live updates."""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QProgressBar, QVBoxLayout, QWidget


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    current_job: str = "No active job"
    progress: float = 0
    files_processed: int = 0
    files_total: int = 0
    original_bytes: int = 0
    final_bytes: int = 0
    stage: str = "Idle"
    elapsed: str = "00:00:00"
    remaining: str = "Calculating ETA..."
    eta: str = "Calculating ETA..."
    cpu: float = 0
    ram: float = 0
    disk: float = 0
    network_down: float = 0
    network_up: float = 0
    workers: str = "0 active"
    stage_etas: dict[str, str] = field(default_factory=dict)


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—") -> None:
        super().__init__(); self.setObjectName("metricCard")
        layout = QVBoxLayout(self); label = QLabel(title.upper()); label.setObjectName("metricTitle")
        self.value_label = QLabel(value); self.value_label.setObjectName("metricValue")
        layout.addWidget(label); layout.addWidget(self.value_label)
    def set_value(self, value: str) -> None: self.value_label.setText(value)


class DashboardPage(QWidget):
    METRICS = (("files_processed", "Files Processed"), ("files_remaining", "Files Remaining"),
        ("original_size", "Original Size"), ("final_size", "Final Size"), ("saved", "Saved"),
        ("reduction", "Reduction"), ("current_stage", "Current Stage"), ("elapsed", "Elapsed"),
        ("remaining", "Remaining"), ("eta", "ETA"), ("cpu", "CPU"), ("ram", "RAM"),
        ("disk", "Disk"), ("network", "Network"), ("workers", "Workers"))

    def __init__(self) -> None:
        super().__init__(); root = QVBoxLayout(self); root.setContentsMargins(30, 26, 30, 24); root.setSpacing(16)
        heading = QLabel("Dashboard"); heading.setObjectName("pageHeading")
        subtitle = QLabel("Live job health, throughput, resources, and predictive timing")
        subtitle.setObjectName("pageSubtitle"); root.addWidget(heading); root.addWidget(subtitle)
        current = QFrame(); current.setObjectName("heroCard"); current_layout = QVBoxLayout(current)
        self.current_job = QLabel("No active job"); self.current_job.setObjectName("currentJobTitle")
        self.stage_line = QLabel("Ready"); self.stage_line.setObjectName("mutedLabel")
        self.progress = QProgressBar(); self.progress.setRange(0, 100); self.progress.setFormat("Ready")
        current_layout.addWidget(QLabel("CURRENT JOB")); current_layout.addWidget(self.current_job)
        current_layout.addWidget(self.stage_line); current_layout.addWidget(self.progress); root.addWidget(current)
        grid = QGridLayout(); grid.setSpacing(12); self.cards = {}
        for index, (key, title) in enumerate(self.METRICS):
            card = MetricCard(title, "—"); self.cards[key] = card; grid.addWidget(card, index // 5, index % 5)
        root.addLayout(grid)
        stage_card = QFrame(); stage_card.setObjectName("panelCard"); stage_layout = QGridLayout(stage_card)
        stage_layout.addWidget(QLabel("STAGE ESTIMATES"), 0, 0, 1, 7); self.stage_labels = {}
        for index, stage in enumerate(("Scan", "Download", "Optimize", "Upload", "Database", "Verification", "Cleanup")):
            label = QLabel(f"{stage}\nCalculating..."); label.setObjectName("stageEstimate")
            self.stage_labels[stage] = label; stage_layout.addWidget(label, 1, index)
        root.addWidget(stage_card); root.addStretch()

    def update_snapshot(self, value: DashboardSnapshot) -> None:
        processed, remaining = value.files_processed, max(0, value.files_total - value.files_processed)
        saved = max(0, value.original_bytes - value.final_bytes)
        reduction = saved / value.original_bytes * 100 if value.original_bytes else 0
        self.current_job.setText(value.current_job); self.progress.setValue(round(value.progress))
        self.progress.setFormat(f"{value.stage}   {processed:,} / {value.files_total:,}   {value.progress:.0f}%")
        self.stage_line.setText(f"Elapsed {value.elapsed}   •   Remaining {value.remaining}   •   ETA {value.eta}")
        values = {"files_processed": f"{processed:,}", "files_remaining": f"{remaining:,}",
            "original_size": _bytes(value.original_bytes), "final_size": _bytes(value.final_bytes),
            "saved": _bytes(saved), "reduction": f"{reduction:.1f}%", "current_stage": value.stage,
            "elapsed": value.elapsed, "remaining": value.remaining, "eta": value.eta,
            "cpu": f"{value.cpu:.0f}%", "ram": f"{value.ram:.0f}%", "disk": f"{value.disk:.0f}%",
            "network": f"↓ {_rate(value.network_down)}  ↑ {_rate(value.network_up)}", "workers": value.workers}
        for key, text in values.items(): self.cards[key].set_value(text)
        for stage, label in self.stage_labels.items(): label.setText(f"{stage}\n{value.stage_etas.get(stage, 'Calculating...')}")

    def update_metric(self, key: str, value: str) -> None:
        if key in self.cards: self.cards[key].set_value(value)

    def update_progress(self, value: float, stage: str) -> None:
        self.progress.setValue(max(0, min(100, round(value)))); self.progress.setFormat(f"{stage}   {value:.0f}%")


def _bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB": return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return "0 B"


def _rate(value: float) -> str: return _bytes(round(value)) + "/s"
