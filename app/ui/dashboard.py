"""Dashboard widgets designed for incremental metric updates."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QProgressBar, QVBoxLayout, QWidget


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—") -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        title_label = QLabel(title.upper())
        title_label.setObjectName("metricTitle")
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)


class DashboardPage(QWidget):
    METRICS = (
        ("files_processed", "Files Processed", "0"),
        ("total_files", "Total Files", "0"),
        ("original_size", "Original Size", "0 B"),
        ("optimized_size", "Optimized Size", "0 B"),
        ("total_saved", "Total Saved", "0 B"),
        ("cpu_usage", "CPU Usage", "—"),
        ("ram_usage", "RAM Usage", "—"),
        ("network_usage", "Network Usage", "—"),
        ("current_stage", "Current Stage", "Idle"),
        ("elapsed_time", "Elapsed Time", "00:00:00"),
        ("remaining_time", "Estimated Remaining", "—"),
    )

    def __init__(self) -> None:
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(18)
        heading = QLabel("Dashboard")
        heading.setObjectName("pageHeading")
        subtitle = QLabel("Overview of image optimization activity")
        subtitle.setObjectName("pageSubtitle")
        root.addWidget(heading)
        root.addWidget(subtitle)

        current = QFrame()
        current.setObjectName("currentJob")
        current_layout = QVBoxLayout(current)
        self.current_job = QLabel("No active job")
        self.current_job.setObjectName("currentJobTitle")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Ready")
        current_layout.addWidget(QLabel("CURRENT JOB"))
        current_layout.addWidget(self.current_job)
        current_layout.addWidget(self.progress)
        root.addWidget(current)

        grid = QGridLayout()
        grid.setSpacing(14)
        self.cards: dict[str, MetricCard] = {}
        for index, (key, title, value) in enumerate(self.METRICS):
            card = MetricCard(title, value)
            self.cards[key] = card
            grid.addWidget(card, index // 3, index % 3)
        root.addLayout(grid)
        root.addStretch()

    def update_metric(self, key: str, value: str) -> None:
        if key in self.cards:
            self.cards[key].set_value(value)

    def update_progress(self, value: float, stage: str) -> None:
        bounded = max(0, min(100, round(value)))
        self.progress.setValue(bounded)
        self.progress.setFormat(f"{stage}  ·  {bounded}%")
        self.update_metric("current_stage", stage)
