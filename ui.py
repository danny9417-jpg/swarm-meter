"""Qt presentation for SWARM METER.

The views consume one sampler snapshot. They do not choose memory thresholds,
lamps, or capacity counts: those arrive from the Lean adapter in backend.py.
Painting a coordinate or formatting a unit is deliberately the only arithmetic
performed on those decisions here. Ordinary Qt controls remain keyboard usable.
"""

from __future__ import annotations

import math
from pathlib import Path
import queue
import threading
import time

from PySide6.QtCore import (
    Qt,
    QAbstractTableModel,
    QModelIndex,
    QRectF,
    QPointF,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableView,
    QHeaderView,
    QAbstractItemView,
    QDialog,
    QFormLayout,
    QComboBox,
    QCheckBox,
    QScrollArea,
    QPlainTextEdit,
)

import backend as motor
from config import VERSION
from runtime import Settings, Notifications, TerminationController, data_directory

COL = dict(
    bg="#101014",
    panel="#19191f",
    raised="#22222a",
    line="#363640",
    text="#f3f1f5",
    secondary="#b5b1bd",
    magenta="#ff5fbf",
    GREEN="#43d477",
    AMBER="#ecab3e",
    RED="#e0563f",
    UNKNOWN="#b5b1bd",
)
# Owner hues identify a swarm without competing with its current risk state.
# The original icon and the family's semantic warning colors remain unchanged.
OWNER_COLOR = {
    "Claude Code": "#f0a24c",
    "Codex": "#6fb3ff",
    "OpenClaw": "#c084fc",
    "Gemini CLI": "#7fe0c0",
    "Docker": "#4dd0e1",
    "Ollama": "#e8e2d4",
    "Orphans": "#e0563f",
    "Desktop": "#a8a5ae",
    "System": "#a8a5ae",
}
FONT = "Noto Sans"

# The UI names the evidence the sampler can actually observe. A logical agent
# may be remote or share a host process; a local process is the measurable unit.
ROLE_LABEL = {
    "workload": "Local jobs",
    "helper": "Helpers",
    "host": "Agent hosts",
    "unclassified": "Unclassified",
}


def owner_breakdown(owner):
    """Keep overhead visible without dividing it into the workload average."""
    return "\n".join(
        f"{title}: {owner.get(role + '_count', 0)} processes · "
        f"{memory(owner.get(role + '_mb', 0))} RAM"
        for role, title in ROLE_LABEL.items()
    )


def memory(value):
    """The sampler and core use MiB, so binary units remain explicit on screen."""
    if value is None or value < 0:
        return "Unavailable"
    return f"{value / 1024:.1f} GiB" if value >= 1024 else f"{value:.0f} MiB"


def elapsed(seconds):
    if seconds is None:
        return "age unavailable"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{int(seconds / 60)} min"
    return f"{int(max(0, seconds))} s"


def font(size=15, bold=False):
    f = QFont(FONT)
    f.setPixelSize(size)
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return f


def label(text="", caption=False, bold=False):
    w = QLabel(text)
    w.setFont(font(12 if caption else 15, bold))
    w.setStyleSheet(f"color:{COL['secondary' if caption else 'text']};")
    w.setTextFormat(Qt.TextFormat.PlainText)
    return w


def button(text, callback=None, accent=False):
    w = QPushButton(text)
    w.setMinimumHeight(32)
    w.setFont(font(12))
    if accent:
        w.setStyleSheet(f"color:{COL['magenta']};")
    if callback:
        w.clicked.connect(callback)
    return w


def style_application(app):
    app.setStyle("Fusion")
    app.setFont(font())
    app.setStyleSheet("""
        QWidget { background:#101014; color:#f3f1f5; }
        QToolTip { background:#22222a; color:#f3f1f5; border:1px solid #363640; padding:8px; }
        QPushButton,QComboBox { background:#22222a; border:1px solid transparent;
            border-radius:8px; padding:5px 10px; }
        QPushButton:hover,QComboBox:hover { background:#303039; }
        QPushButton:focus,QComboBox:focus,QCheckBox:focus { border:1px solid #ff5fbf; }
        QPushButton:disabled { color:#8c8793; }
        QTableView { border:0; background:#101014; selection-background-color:#303039;
            gridline-color:#22222a; outline:0; }
        QHeaderView::section { background:#101014; color:#b5b1bd; border:0;
            padding:8px 6px; }
        QScrollArea { border:0; }
        QScrollBar:vertical { width:8px; background:#101014; }
        QScrollBar::handle:vertical { background:#363640; border-radius:4px; min-height:24px; }
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
        QPlainTextEdit { background:#19191f; border:1px solid #363640;
            border-radius:8px; padding:8px; }
        QCheckBox { spacing:8px; }
    """)


class Painted(QWidget):
    """Shared drawing helpers reserve space and elide long labels safely."""

    def text(
        self,
        p,
        x,
        y,
        text,
        size=15,
        color="text",
        bold=False,
        width=None,
        align=Qt.AlignmentFlag.AlignLeft,
    ):
        p.setFont(font(size, bold))
        p.setPen(QColor(COL.get(color, color)))
        if width is not None:
            text = p.fontMetrics().elidedText(
                str(text), Qt.TextElideMode.ElideRight, int(width)
            )
            p.drawText(
                QRectF(x, y - size - 4, width, size + 12),
                align | Qt.AlignmentFlag.AlignVCenter,
                text,
            )
        else:
            p.drawText(QPointF(x, y), str(text))

    def box(self, p, x, y, w, h, color="panel", radius=12):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(COL.get(color, color)))
        p.drawRoundedRect(QRectF(x, y, w, h), radius, radius)

    def line(self, p, x, y, x2, y2, color="line", width=1, dashed=False):
        pen = QPen(QColor(COL.get(color, color)), width)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(x, y), QPointF(x2, y2))

    def meter(self, p, x, y, w, snap):
        total = snap["mem"]["total"]
        available = snap["mem"]["available"]
        core = snap["core"]
        self.box(p, x, y, w, 12, "raised", 6)
        if total <= 0 or not core.get("valid", True):
            self.text(p, x, y + 38, "Memory reading unavailable", 12, "secondary")
            return
        # All decision values are supplied by the core. This only maps them to pixels.
        red = core["redLine"] / total * w
        amber = core["amberLine"] / total * w
        current = max(0, min(w, available / total * w))
        self.box(p, x, y, red, 12, "#522820", 6)
        self.box(p, x + red, y, max(0, amber - red), 12, "#4b3b22", 0)
        self.box(p, x, y + 3, current, 6, core["level"], 3)
        self.line(p, x + red, y - 5, x + red, y + 17, "RED", 2)
        self.line(p, x + amber, y - 5, x + amber, y + 17, "AMBER", 2)
        self.line(p, x + current, y - 7, x + current, y + 19, "text", 2)
        self.text(p, x, y + 37, "0 MiB", 12, "secondary")
        self.text(
            p,
            x,
            y + 37,
            f"{memory(total)} total",
            12,
            "secondary",
            width=w,
            align=Qt.AlignmentFlag.AlignRight,
        )
        self.text(p, x, y + 60, f"Red < {memory(core['redLine'])}", 12, "RED")
        self.text(
            p,
            x,
            y + 60,
            f"Amber < {memory(core['amberLine'])}",
            12,
            "AMBER",
            width=w,
            align=Qt.AlignmentFlag.AlignRight,
        )


class Glance(Painted):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFixedSize(424, 336)
        self.setAccessibleName(
            "Glance: risk, available memory, capacity and leading swarm"
        )

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        app = self.owner
        s = app.snap
        if not s:
            self.text(p, 16, 56, "Reading the machine…", 15)
            self.text(
                p, 16, 84, "Waiting for the first complete sample.", 12, "secondary"
            )
            return
        level = s["core"]["level"]
        stale = app.is_stale()
        # Stable neutral surfaces avoid a whole-card flash when risk changes.
        # Only the small status rail and warning word carry the semantic color.
        self.box(p, 0, 8, 424, 72, "panel", 0)
        self.box(p, 0, 8, 3, 72, "AMBER" if stale else level, 0)
        self.text(
            p,
            16,
            43,
            "STALE" if stale else level,
            32,
            "AMBER" if stale else level,
            True,
        )
        self.text(p, 16, 66, app.status_sentence(), 15, width=392)
        self.text(p, 16, 94, "Available RAM", 12, "secondary")
        self.text(p, 16, 128, memory(s["mem"]["available"]), 32)
        total = s["mem"]["total"]
        available = s["mem"]["available"]
        pct = (
            f"{available / total:.0%} free"
            if total > 0 and available >= 0
            else "Unavailable"
        )
        self.text(
            p,
            190,
            125,
            f"of {memory(total)} · {pct}",
            12,
            "secondary",
            width=218,
            align=Qt.AlignmentFlag.AlignRight,
        )
        self.meter(p, 16, 144, 392, s)
        owner = app.capacity_owner()
        cap = app.capacity()
        self.text(p, 16, 230, app.capacity_title(cap), 15, "text", True, width=392)
        n = owner["count"] if owner else len(s["agent_jobs"])
        basis = app.capacity_size()
        approx = "≈ " if owner and owner.get("workload_memory_estimated") else ""
        self.text(
            p,
            16,
            252,
            (
                f"{n} local jobs · {approx}{memory(basis)} / job {app.basis_label()}"
                if basis is not None
                else app.no_workload_summary(owner)
            ),
            12,
            "secondary",
            width=392,
        )
        self.text(p, 16, 274, app.trend_line(), 12, "secondary", width=392)
        workload = (
            max(owner["jobs"], key=lambda j: j["mem"] or 0)["name"]
            if owner and owner["jobs"]
            else ""
        )
        self.text(
            p,
            16,
            295,
            ("Selected swarm" if app.owner_key else "Largest swarm")
            + (" · " + workload if workload else ""),
            12,
            "secondary",
            width=392,
        )
        owner_name = (
            owner["label"]
            + (
                " · " + elapsed(owner.get("age_s"))
                if owner.get("age_s") is not None
                else ""
            )
            if owner
            else "No local workload identified"
        )
        self.text(p, 16, 318, owner_name, 15, width=392)
        self.setToolTip(
            app.core_line()
            + "\n"
            + app.trend_line()
            + "\n"
            + (owner["label"] if owner else "No active swarm")
        )


class Overview(Painted):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFixedHeight(240)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        app = self.owner
        s = app.snap
        w = self.width()
        if not s:
            self.text(p, 24, 52, "Reading the machine…", 32)
            return
        level = s["core"]["level"]
        stale = app.is_stale()
        self.box(p, 0, 0, w, 128, "panel")
        self.box(p, 0, 10, 3, 108, "AMBER" if stale else level, 0)
        self.text(
            p,
            24,
            48,
            "STALE" if stale else level,
            32,
            "AMBER" if stale else level,
            True,
        )
        self.text(p, 24, 78, app.status_sentence(), 15, "text", True, width=w - 300)
        self.text(
            p,
            24,
            99,
            app.summary.get("ramp_text") or "Local workload monitoring",
            12,
            "secondary",
            width=w - 300,
        )
        self.text(
            p,
            w - 280,
            28,
            "Available RAM",
            12,
            "secondary",
            width=256,
            align=Qt.AlignmentFlag.AlignRight,
        )
        self.text(
            p,
            w - 280,
            68,
            memory(s["mem"]["available"]),
            32,
            width=256,
            align=Qt.AlignmentFlag.AlignRight,
        )
        total = s["mem"]["total"]
        available = s["mem"]["available"]
        pct = (
            f"{available / total:.0%} free"
            if total > 0 and available >= 0
            else "Unavailable"
        )
        self.text(
            p,
            w - 280,
            94,
            f"of {memory(total)} · {pct}",
            12,
            "secondary",
            width=256,
            align=Qt.AlignmentFlag.AlignRight,
        )
        self.line(p, 24, 106, w - 24, 106)
        self.text(
            p, 24, 124, app.capacity_title(app.capacity()), 15, "text", True, width=340
        )
        selected = app.capacity_owner()
        n = selected["count"] if selected else 0
        self.text(
            p,
            360,
            124,
            (
                f"{memory(app.capacity_size())} / job {app.basis_label()} · {n} local jobs"
                if app.capacity_size() is not None
                else app.no_workload_summary(selected)
            ),
            12,
            "secondary",
            width=w - 384,
            align=Qt.AlignmentFlag.AlignRight,
        )
        self.meter(p, 0, 144, w, s)
        self.text(p, 0, 234, app.trend_line(), 12, "secondary", width=w)


class Resources(Painted):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFixedHeight(72)

    def paintEvent(self, event):
        p = QPainter(self)
        s = self.owner.snap
        if not s:
            return
        gpu = s["gpu"]
        pr = s["pressure"]
        mem = s["mem"]
        third = self.width() / 3
        age = s.get("gpu_age_s")
        gpu_note = (
            f"{gpu['used'] / max(1, gpu['total']):.0%} used · read {elapsed(age)} ago"
            if gpu
            else (
                "Reading driver…"
                if s.get("gpu_pending")
                else "Driver reading unavailable"
            )
        )
        values = [
            (
                f"GPU memory · {gpu['device_count']} cards"
                if gpu and gpu.get("device_count", 1) > 1
                else "GPU memory",
                f"{memory(gpu['used'])} / {memory(gpu['total'])}"
                if gpu
                else "Unavailable",
                gpu_note,
            ),
            (
                "Memory stalls",
                f"{pr / 10:.1f}% of time waiting" if pr is not None else "Unavailable",
                "Observed over the last 10 s",
            ),
            (
                "Swap",
                f"{memory(mem['swap_total'] - mem['swap_free'])} / {memory(mem['swap_total'])}",
                "Backup memory used",
            ),
        ]
        self.line(p, 0, 0, self.width(), 0)
        for i, (title, value, note) in enumerate(values):
            x = i * third
            self.text(p, x, 23, title, 12, "secondary")
            self.text(p, x, 48, value, 15, "text", True, width=third - 16)
            self.text(p, x, 70, note, 12, "secondary", width=third - 16)


class Hive(Painted):
    selected = Signal(object)

    def __init__(self):
        super().__init__()
        self.setFixedHeight(160)
        self.jobs = []
        self.slots = {}
        self.hit = []
        self.current = None
        self.setMouseTracking(True)
        self.setAccessibleName(
            "Identified local workload processes; keyboard-accessible list in owner table"
        )

    def set_jobs(self, jobs, current):
        self.jobs = jobs
        self.current = current
        active = {tuple(j["identity"]) for j in jobs}
        self.slots = {k: v for k, v in self.slots.items() if k in active}
        occupied = set(self.slots.values())
        for j in jobs:
            key = tuple(j["identity"])
            if key not in self.slots:
                slot = next(i for i in range(len(jobs) + 1) if i not in occupied)
                self.slots[key] = slot
                occupied.add(slot)
        # Preserve every visible survivor's position. Jobs that used to be in
        # overflow move into visible vacancies after a swarm shrinks; otherwise
        # the hive could look empty while real jobs remained off-screen.
        visible_limit = max(1, self.width() // 51) * 3
        vacant = iter(i for i in range(visible_limit) if i not in occupied)
        for key, slot in list(self.slots.items()):
            if slot >= visible_limit:
                destination = next(vacant, None)
                if destination is None:
                    break
                self.slots[key] = destination
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.hit = []
        if not self.jobs:
            self.text(p, 8, 75, "No local workload identified", 15, "text", True)
            self.text(
                p,
                8,
                105,
                "Hosts and helpers remain in the owner list.",
                12,
                "secondary",
            )
            return
        columns = max(1, self.width() // 51)
        shown = 0
        for j in self.jobs:
            index = self.slots[tuple(j["identity"])]
            row, col = divmod(index, columns)
            if row >= 3:
                continue
            x = 26 + col * 51
            y = 26 + row * 44
            radius = 23
            color = OWNER_COLOR.get(j["kind"], COL["secondary"])
            fill = QColor(color)
            fill.setAlpha(42)
            ring = (
                COL["RED"]
                if j.get("orphan_evidence")
                else COL["magenta"]
                if j.get("guarded")
                else color
            )
            p.setPen(
                QPen(
                    QColor(ring),
                    2 if j.get("guarded") or j.get("orphan_evidence") else 1,
                )
            )
            p.setBrush(fill)
            points = QPolygonF(
                [
                    QPointF(
                        x + radius * math.cos(math.radians(i * 60 - 30)),
                        y + radius * math.sin(math.radians(i * 60 - 30)),
                    )
                    for i in range(6)
                ]
            )
            p.drawPolygon(points)
            if tuple(j["identity"]) == self.current:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(COL["text"]), 2))
                p.drawEllipse(QPointF(x, y), 17, 17)
            if j["mem"] is not None and j["mem"] >= 1024:
                self.text(
                    p,
                    x - 24,
                    y - 1,
                    f"{j['mem'] / 1024:.1f}",
                    12,
                    width=48,
                    align=Qt.AlignmentFlag.AlignCenter,
                )
                self.text(
                    p,
                    x - 24,
                    y + 14,
                    "GiB",
                    12,
                    "secondary",
                    width=48,
                    align=Qt.AlignmentFlag.AlignCenter,
                )
            self.hit.append((QRectF(x - 24, y - 24, 48, 48), j))
            shown += 1
        if shown < len(self.jobs):
            self.text(
                p,
                0,
                157,
                f"+{len(self.jobs) - shown} jobs in the owner list",
                12,
                "secondary",
            )

    def mousePressEvent(self, event):
        for r, j in self.hit:
            if r.contains(event.position()):
                self.selected.emit(j)
                break

    def mouseMoveEvent(self, event):
        for r, j in self.hit:
            if r.contains(event.position()):
                self.setToolTip(
                    f"{j['kind']} · {j['name']} · {memory(j['mem'])}\nPID {j['pid']} · click to inspect"
                )
                return
        self.setToolTip("")


class Scope(Painted):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.setFixedHeight(160)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.owner.snap
        if not s:
            return
        self.text(p, 0, 15, "Available RAM", 12, "magenta")
        self.text(p, 160, 15, "Known agent RAM (dashed)", 12, "secondary")
        x, y, w, h = 58, 30, max(1, self.width() - 66), 98
        total = max(1, s["mem"]["total"])
        for fraction in (0, 0.5, 1):
            yy = y + h * (1 - fraction)
            self.line(p, x, yy, x + w, yy)
            self.text(
                p,
                0,
                yy + 4,
                memory(total * fraction),
                12,
                "secondary",
                width=50,
                align=Qt.AlignmentFlag.AlignRight,
            )
        core = s["core"]
        if core.get("valid", True):
            for key, color in [("redLine", "RED"), ("amberLine", "AMBER")]:
                yy = y + h * (1 - core[key] / total)
                self.line(p, x, yy, x + w, yy, color, 1, True)
        now = s["ts"]
        series = self.owner.trends.series
        for idx, color, dash in [(1, "magenta", False), (2, "secondary", True)]:
            pts = []
            for row in series:
                if row[idx] is not None and row[idx] >= 0:
                    pts.append(
                        QPointF(
                            x + w * max(0, min(1, 1 - (now - row[0]) / 300)),
                            y + h * (1 - max(0, min(total, row[idx])) / total),
                        )
                    )
            if len(pts) > 1:
                pen = QPen(QColor(COL[color]), 2)
                if dash:
                    pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPolyline(QPolygonF(pts))
        self.text(p, x, 154, "5 min ago", 12, "secondary")
        self.text(
            p,
            x,
            154,
            "Now",
            12,
            "secondary",
            width=w,
            align=Qt.AlignmentFlag.AlignRight,
        )


class Owners(QAbstractTableModel):
    """Rows retain owner identity; sampling never resizes or sorts under a click."""

    headers = [
        "Owner · folder",
        "RAM",
        "Local jobs",
        "Helpers",
        "Avg / job",
        "GPU RAM",
        "Trend",
        "RAM share",
    ]

    def __init__(self):
        super().__init__()
        self.rows = []
        self.previous = {}
        self.changes = {}
        self.total = 1

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return self.headers[section]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(
                Qt.AlignmentFlag.AlignVCenter
                | (
                    Qt.AlignmentFlag.AlignLeft
                    if section == 0
                    else Qt.AlignmentFlag.AlignRight
                )
            )

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        o = self.rows[index.row()]
        c = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            approx = "≈ " if o.get("memory_estimated") else ""
            total_prefix = "≥ " if not o.get("memory_complete", True) else approx
            mean_prefix = "≈ " if o.get("workload_memory_estimated") else ""
            owner_name = o["label"] + (
                " · " + elapsed(o.get("age_s")) if o.get("age_s") is not None else ""
            )
            vals = [
                owner_name,
                total_prefix + memory(o["mem"]),
                f"{o['count']} jobs",
                f"{o.get('helper_count', 0)} procs",
                mean_prefix + memory(o.get("mean_mb")) if o["count"] else "—",
                memory(o.get("vram")),
                self.changes.get(o["key"], "New"),
                f"{o['mem'] / self.total:.0%}",
            ]
            return vals[c]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(
                Qt.AlignmentFlag.AlignVCenter
                | (
                    Qt.AlignmentFlag.AlignLeft
                    if c == 0
                    else Qt.AlignmentFlag.AlignRight
                )
            )
        if role == Qt.ItemDataRole.ForegroundRole and c == 0:
            return QColor(OWNER_COLOR.get(o["kind"], COL["secondary"]))
        if role == Qt.ItemDataRole.DecorationRole and c == 0:
            return QColor(OWNER_COLOR.get(o["kind"], COL["secondary"]))
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"{o['label']}\n{o.get('cwd', '')}\n"
                + owner_breakdown(o)
                + f"\nRunning {elapsed(o.get('age_s'))}. Memory accounting age: {elapsed(o.get('memory_age_s', 0))}.\n"
                "≈ means cached proportional RAM plus observed resident-memory growth.\n"
                "≥ means some process RAM is unreadable; this total is a known minimum.\n"
                "Local jobs are identified workload processes, not logical agents.\nSelect to inspect every process."
            )

    def apply(self, s):
        mapping = {o["key"]: o for o in s["owners"]}
        new = [mapping[o["key"]] for o in self.rows if o["key"] in mapping]
        existing = {o["key"] for o in new}
        new.extend(o for o in s["owners"] if o["key"] not in existing)
        self.total = max(1, s["mem"]["total"])
        for o in new:
            old = self.previous.get(o["key"])
            if old and s["ts"] > old[0]:
                rate = (o["mem"] - old[1]) * 60 / (s["ts"] - old[0])
                self.changes[o["key"]] = (
                    f"{'↑' if rate > 0 else '↓'} {memory(abs(rate))}/min"
                    if abs(rate) > 16
                    else "Stable"
                )
            else:
                self.changes[o["key"]] = "New"
            self.previous[o["key"]] = (s["ts"], o["mem"])
        if [o["key"] for o in self.rows] != [o["key"] for o in new]:
            self.beginResetModel()
            self.rows = new
            self.endResetModel()
        else:
            self.rows = new
            if new:
                self.dataChanged.emit(
                    self.index(0, 0), self.index(len(new) - 1, len(self.headers) - 1)
                )


class OwnerInspector(QDialog):
    """Refresh an open swarm without replacing unchanged process buttons.

    Helpers stay inspectable even when the hive is empty. Stable identities also
    prevent a refresh from changing the target beneath a pending mouse click.
    """

    def __init__(self, owner, key):
        super().__init__(owner)
        self.owner = owner
        self.key = key
        self.rows = {}
        self.resize(580, 560)
        layout = QVBoxLayout(self)
        self.summary = label(bold=True)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.breakdown = label(caption=True)
        self.breakdown.setWordWrap(True)
        layout.addWidget(self.breakdown)
        note = label(
            "Local jobs count identified workload processes. Helpers and agent hosts "
            "use RAM too; they are excluded from the job average. Unclassified "
            "processes remain visible below. A helper may itself be busy; its "
            "internal requests cannot be counted from process presence.",
            caption=True,
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.process_layout = QVBoxLayout(content)
        self.process_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)
        layout.addWidget(button("Close", self.accept))
        self.refresh()

    def refresh(self):
        o = self.owner.find_owner(self.key)
        if not o:
            self.summary.setText("This swarm is no longer observed.")
            for b in self.rows.values():
                b.setEnabled(False)
            return
        self.setWindowTitle(o["label"])
        mean = (
            memory(o.get("mean_mb")) + " average RAM/job"
            if o["count"]
            else "No measured job size"
        )
        self.summary.setText(f"{o['count']} local jobs · {mean}")
        self.breakdown.setText(owner_breakdown(o))
        processes = o.get("processes", o["jobs"])
        by_id = {tuple(j["identity"]): j for j in processes}
        for identity in list(self.rows):
            if identity not in by_id:
                b = self.rows.pop(identity)
                self.process_layout.removeWidget(b)
                b.deleteLater()
        for j in sorted(processes, key=lambda item: -(item["mem"] or 0)):
            identity = tuple(j["identity"])
            b = self.rows.get(identity)
            if b is None:
                b = button("")
                b.clicked.connect(
                    lambda checked=False, target=identity: self.choose(target)
                )
                self.process_layout.insertWidget(self.process_layout.count() - 1, b)
                self.rows[identity] = b
            role = ROLE_LABEL.get(j.get("role"), "Process")
            b.setText(f"{role} · {j['name']} · {memory(j['mem'])} · PID {j['pid']}")
            b.setToolTip(j.get("role_reason", "Role not classified"))

    def choose(self, identity):
        o = self.owner.find_owner(self.key)
        j = (
            next(
                (
                    p
                    for p in o.get("processes", o["jobs"])
                    if tuple(p["identity"]) == identity
                ),
                None,
            )
            if o
            else None
        )
        if j:
            self.accept()
            self.owner.inspect_job(j)


class Inspector(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.job = None
        self.setWindowTitle("Process inspector — Swarm Meter")
        self.resize(464, 760)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        self.title = label("", bold=True)
        self.title.setFont(font(32, True))
        layout.addWidget(self.title)
        self.session = label()
        self.session.setWordWrap(True)
        layout.addWidget(self.session)
        self.identity = label(caption=True)
        layout.addWidget(self.identity)
        self.memory = label()
        self.memory.setFont(font(32))
        layout.addWidget(self.memory)
        self.gpu = label(caption=True)
        layout.addWidget(self.gpu)
        self.chain = label()
        self.chain.setWordWrap(True)
        layout.addWidget(label("Spawned by", caption=True))
        layout.addWidget(self.chain)
        layout.addWidget(label("Full command", caption=True))
        self.command = QPlainTextEdit()
        self.command.setReadOnly(True)
        self.command.setMinimumHeight(80)
        self.command.setMaximumHeight(120)
        layout.addWidget(self.command)
        self.status = label(caption=True)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.capacity_label = label(caption=True)
        self.capacity_label.setWordWrap(True)
        layout.addWidget(self.capacity_label)
        self.details = button("Advanced details", self.show_advanced, True)
        layout.addWidget(self.details)
        layout.addStretch(1)
        layout.addWidget(label("Stop this process", bold=True))
        warning = label(
            "Unsaved work may be lost. Memory is released only if the job exits.",
            caption=True,
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.stop = button("TERMINATE…", self.action)
        self.stop.setStyleSheet("background:#3d2422;color:#e0563f;")
        layout.addWidget(self.stop)
        self.action_status = label(
            "Arm, then confirm this exact job within 4 s.", caption=True
        )
        self.action_status.setWordWrap(True)
        self.action_status.setMinimumHeight(40)
        layout.addWidget(self.action_status)
        self.cancel = button("Cancel confirmation", self.cancel_action)
        layout.addWidget(self.cancel)
        # Escape/reject can hide a QDialog without closeEvent. Every dismissal
        # must disarm, so reopening the same job always starts with an arm click.
        self.finished.connect(lambda _result: self.owner.termination.cancel())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_action)
        self.timer.start(200)

    def set_job(self, j):
        self.job = j
        self.owner.termination.select(j, demo=self.owner.preview)
        self.action_status.setText("Arm, then confirm this exact job within 4 s.")
        self.refresh_job(j)
        self.refresh_action()

    def refresh_job(self, j):
        self.job = j
        self.title.setText(j["name"])
        self.session.setText(self.owner.owner_label(j["owner"]))
        # exec() can replace a process's program without changing its identity.
        # Avoid resetting an unchanged command, preserving text selection.
        if self.command.toPlainText() != j["cmd"]:
            self.command.setPlainText(j["cmd"])
        self.identity.setText(f"PID {j['pid']} · running {elapsed(j.get('age_s'))}")
        self.memory.setText(memory(j["mem"]) + " RAM")
        self.gpu.setText("GPU memory: " + memory(j.get("vram")))
        self.chain.setText(" › ".join(j["chain"]))
        evidence = j.get("orphan_evidence")
        host = (
            evidence["label"]
            if evidence
            else "Host connected"
            if j["kind"] in motor.AGENT_KINDS
            else "No agent host attributed"
        )
        self.status.setText(
            ROLE_LABEL.get(j.get("role"), "Process")
            + " · "
            + host
            + " · "
            + ("Guard detected" if j.get("guarded") else "Guard not detected")
        )
        o = self.owner.find_owner(j["owner"])
        self.capacity_label.setText(
            (
                f"Swarm: {o['count']} local jobs · {memory(o.get('mean_mb'))} average RAM/job"
                if o["count"]
                else "Swarm: no local workload identified; capacity unavailable"
            )
            if o
            else ""
        )

    def show_advanced(self):
        j = self.job
        if not j:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Measurement details")
        dialog.resize(520, 430)
        lay = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        evidence = j.get("orphan_evidence") or {}
        text.setPlainText(
            "PSS: shared RAM pages are divided proportionally between processes.\n"
            "RSS: resident memory, used when proportional accounting is unavailable.\n"
            "PSI: kernel measurement of time tasks wait for memory.\n"
            "Tenths: the core transport uses 10 units for 1% waiting.\n"
            "cgroup: an operating-system process group, sometimes with resource limits.\n\n"
            "Local jobs: identified workload processes, including small jobs and workers waiting for GPU, disk or network.\n"
            "These are not logical-agent or cloud-task counts. Unknown programs stay unclassified.\n"
            "Helper is a service role, not an idle-state measurement; internal service requests are not observable here.\n"
            "Average RAM/job: workload RAM divided by workload processes. Hosts, helpers and unclassified RAM are excluded.\n"
            "Capacity is shared across the machine; different swarm estimates cannot be added.\n\n"
            f"Process role: {j.get('role', 'unclassified')}\nEvidence: {j.get('role_reason', 'Unavailable')}\n\n"
            f"Memory source: {j.get('memory_kind', 'unavailable')}\nMeasurement age: {elapsed(j.get('memory_age_s', 0))}\n"
            f"Working folder: {j.get('cwd', '')}\n"
            f"Orphan evidence: {evidence.get('reason', 'No orphan evidence.')}\n\n"
            + self.owner.core_line()
        )
        lay.addWidget(text)
        lay.addWidget(button("Close", dialog.accept))
        dialog.exec()

    def action(self):
        result = self.owner.termination.click()
        self.action_status.setText(result.message)
        self.refresh_action()

    def cancel_action(self):
        self.action_status.setText(self.owner.termination.cancel().message)
        self.refresh_action()

    def refresh_action(self):
        ctrl = self.owner.termination
        self.cancel.setEnabled(ctrl.armed)
        if ctrl.armed:
            self.stop.setText(
                f"CONFIRM stop · PID {ctrl.selected.pid} · {ctrl.remaining:.1f} s"
            )
        else:
            self.stop.setText("TERMINATE…")
            if ctrl.last_result.status == "expired":
                self.action_status.setText(ctrl.last_result.message)
        self.stop.setEnabled(not self.owner.preview and ctrl.selected is not None)
        if self.owner.preview:
            self.action_status.setText(
                "Demo / screenshot: process actions are disabled."
            )

    def closeEvent(self, event):
        self.owner.termination.cancel()
        super().closeEvent(event)


class SwarmWindow(QWidget):
    """One current snapshot drives all views; worker results are bounded to one."""

    def __init__(self, preview=False, view=None, data_dir=None, start_worker=True):
        super().__init__()
        self.preview = preview
        self.snap = None
        self.summary = {}
        self.owner_key = None
        self.selected_identity = None
        self.settings = Settings(data_dir)
        self.state = self.settings.load()
        try:
            legacy = int(self.state.get("interval_idx", 1))
            self.interval = int(
                self.state.get("interval", [1, 2, 4][min(2, max(0, legacy))])
            )
        except (TypeError, ValueError):
            self.interval = 2
        if self.interval not in (1, 2, 4):
            self.interval = 2
        self.capacity_basis = self.state.get("capacity_basis", "mean")
        if self.capacity_basis not in ("mean", "planning"):
            self.capacity_basis = "mean"
        self.view = view or (
            "glance" if self.state.get("collapsed", True) else "expanded"
        )
        self.pinned = bool(self.state.get("pinned", False))
        self.notifications = Notifications(enabled=bool(self.state.get("notify", True)))
        self.termination = TerminationController(mode="demo" if preview else "live")
        self.trends = motor.TrendTracker()
        self.sampler = None
        self.history = None
        self.history_status = "Preview" if preview else "Starting"
        self.stop_event = threading.Event()
        self.q = queue.Queue(maxsize=1)
        self.worker = None
        self.started = time.monotonic()
        self.first_sample_s = None
        self.last_error = ""
        self.inspector = None
        self.capture_metrics = []
        self.setWindowTitle("FUTURE GADGET No.21 — Swarm Meter")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("icon.png"))))
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.pinned)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(0, 0, 0, 0)
        self.root.setSpacing(0)
        header = QWidget()
        header.setFixedHeight(48)
        h = QHBoxLayout(header)
        h.setContentsMargins(16, 4, 16, 4)
        h.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(self.windowIcon().pixmap(24, 24))
        h.addWidget(icon)
        h.addWidget(label("Swarm Meter", bold=True))
        h.addStretch()
        self.pin = button("Pinned" if self.pinned else "Pin", self.toggle_pin)
        self.pin.setToolTip("Keep this window above other windows")
        h.addWidget(self.pin)
        self.expand = button("Open", self.toggle_view, True)
        h.addWidget(self.expand)
        self.settings_btn = button("⋯", self.show_settings)
        self.settings_btn.setFixedWidth(32)
        self.settings_btn.setAccessibleName("Settings and machine details")
        h.addWidget(self.settings_btn)
        self.root.addWidget(header)
        self.glance = Glance(self)
        self.root.addWidget(self.glance)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(24, 8, 24, 8)
        self.body_layout.setSpacing(8)
        self.overview = Overview(self)
        self.body_layout.addWidget(self.overview)
        self.resources = Resources(self)
        self.body_layout.addWidget(self.resources)
        mid = QHBoxLayout()
        mid.setSpacing(24)
        left = QVBoxLayout()
        left.setSpacing(4)
        self.hive_title = label("The swarm", bold=True)
        left.addWidget(self.hive_title)
        self.hive = Hive()
        self.hive.selected.connect(self.inspect_job)
        left.addWidget(self.hive)
        self.legend = label(caption=True)
        self.legend.setWordWrap(True)
        self.legend.setFixedHeight(40)
        left.addWidget(self.legend)
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addWidget(label("Memory · last 5 min", bold=True))
        self.scope = Scope(self)
        right.addWidget(self.scope)
        rings = label(
            "Red ring: host gone / possible orphan\nPink ring: guard detected",
            caption=True,
        )
        rings.setFixedHeight(40)
        right.addWidget(rings)
        mid.addLayout(left, 1)
        mid.addLayout(right, 1)
        mid.setContentsMargins(0, 0, 0, 0)
        mid_frame = QWidget()
        mid_frame.setLayout(mid)
        mid_frame.setFixedHeight(232)
        self.body_layout.addWidget(mid_frame)
        tablehead = QHBoxLayout()
        tablehead.setContentsMargins(0, 0, 0, 0)
        tablehead.addWidget(label("Who is using it", bold=True))
        tablehead.addStretch()
        self.role_summary = label("Open an owner for all process roles", caption=True)
        tablehead.addWidget(self.role_summary)
        table_frame = QWidget()
        table_frame.setLayout(tablehead)
        table_frame.setFixedHeight(24)
        self.body_layout.addWidget(table_frame)
        self.model = Owners()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setFixedHeight(220)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        header = self.table.horizontalHeader()
        header.setFont(font(12))
        header.setFixedHeight(32)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for i, width in enumerate([238, 90, 84, 72, 92, 84, 112, 84]):
            self.table.setColumnWidth(i, width)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setFont(font(12))
        self.table.clicked.connect(self.inspect_owner)
        self.table.activated.connect(self.inspect_owner)
        self.body_layout.addWidget(self.table)
        advice = QHBoxLayout()
        advice.setContentsMargins(0, 0, 0, 0)
        self.advice = label("Waiting for a reading", bold=True)
        advice.addWidget(self.advice)
        advice.addStretch()
        advice.addWidget(button("Inspect largest job", self.inspect_largest, True))
        advice_frame = QWidget()
        advice_frame.setLayout(advice)
        advice_frame.setFixedHeight(40)
        self.body_layout.addWidget(advice_frame)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(body)
        self.root.addWidget(self.scroll, 1)
        self.footer = label(caption=True)
        self.footer.setContentsMargins(16, 0, 16, 0)
        self.footer.setFixedHeight(24)
        self.root.addWidget(self.footer)
        self.state_timer = QTimer(self)
        self.state_timer.setSingleShot(True)
        self.state_timer.timeout.connect(self.persist)
        self.tick = QTimer(self)
        self.tick.timeout.connect(self.drain)
        self.tick.start(250)
        self.set_view(self.view, save=False)
        self.restore_position()
        if start_worker and not preview:
            self.worker = threading.Thread(
                target=self.sample_loop, name="swarm-sampler", daemon=True
            )
            self.worker.start()

    def set_view(self, view, save=True):
        self.view = view
        is_glance = view == "glance"
        self.glance.setVisible(is_glance)
        self.scroll.setVisible(not is_glance)
        self.expand.setText("Open" if is_glance else "Glance")
        screen = (
            self.screen().availableGeometry()
            if self.screen()
            else QApplication.primaryScreen().availableGeometry()
        )
        self.setFixedSize(424, 408) if is_glance else self.setFixedSize(
            min(960, screen.width() - 32), min(960, screen.height() - 64)
        )
        # Expanding near a screen edge must keep the entire view reachable.
        # Use the current screen; never jump to a different monitor on a sample.
        frame = self.frameGeometry()
        self.move(
            max(screen.left(), min(frame.left(), screen.right() - frame.width() + 1)),
            max(screen.top(), min(frame.top(), screen.bottom() - frame.height() + 1)),
        )
        if save:
            self.persist()

    def toggle_view(self):
        self.set_view("expanded" if self.view == "glance" else "glance")

    def toggle_pin(self):
        self.pinned = not self.pinned
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.pinned)
        self.show()
        self.pin.setText("Pinned" if self.pinned else "Pin")
        self.persist()

    def restore_position(self):
        pos = self.state.get("position")
        if not pos and isinstance(self.state.get("geometry"), str):
            import re

            match = re.fullmatch(r"\+(-?\d+)\+(-?\d+)", self.state["geometry"])
            if match:
                pos = list(map(int, match.groups()))
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            try:
                screen = self.screen().availableGeometry()
                self.move(
                    max(screen.left(), min(int(pos[0]), screen.right() - self.width())),
                    max(
                        screen.top(), min(int(pos[1]), screen.bottom() - self.height())
                    ),
                )
            except (TypeError, ValueError):
                pass

    def moveEvent(self, event):
        if hasattr(self, "state_timer") and not self.preview:
            self.state_timer.start(400)
        super().moveEvent(event)

    def persist(self):
        if self.preview:
            return
        self.settings.update(
            position=[self.x(), self.y()],
            geometry=f"+{self.x()}+{self.y()}",
            collapsed=self.view == "glance",
            pinned=self.pinned,
            interval=self.interval,
            interval_idx=[1, 2, 4].index(self.interval),
            notify=self.notifications.enabled,
            capacity_basis=self.capacity_basis,
        )

    def sample_loop(self):
        # SQLite is created and used on this same worker thread. The UI never waits
        # for /proc, the GPU tool, disk writes, or the core's pipe.
        self.sampler = motor.Sampler()
        history = motor.History(
            str(data_directory(self.settings.directory) / "history.db")
        )
        self.history = history
        try:
            while not self.stop_event.is_set():
                start = time.monotonic()
                try:
                    snap = self.sampler.snapshot()
                    history.add(snap)
                    snap["history_status"] = history.status
                    payload = snap
                except Exception as exc:
                    payload = {"error": str(exc), "ts": time.time()}
                try:
                    self.q.get_nowait()
                except queue.Empty:
                    pass
                self.q.put_nowait(payload)
                self.stop_event.wait(
                    max(0.05, self.interval - (time.monotonic() - start))
                )
        finally:
            history.close()
            self.sampler.close()

    def drain(self):
        try:
            payload = self.q.get_nowait()
        except queue.Empty:
            payload = None
        if payload:
            if "error" in payload:
                self.last_error = payload["error"]
            else:
                self.apply_snapshot(payload)
        self.update_footer()
        if self.is_stale():
            self.glance.update()
            self.overview.update()

    def apply_snapshot(self, s):
        self.snap = s
        self.last_error = ""
        self.summary = self.trends.add(s)
        self.history_status = s.get(
            "history_status", "Preview" if self.preview else "Recording"
        )
        if self.first_sample_s is None:
            self.first_sample_s = time.monotonic() - self.started
        # Workload disappearance is governed by the authoritative job list.
        # Other process roles remain inspectable, including helpers under an
        # agent host, without resurrecting a vanished job from stale row data.
        observed_jobs = list(s["agent_jobs"]) + [
            j
            for o in s["owners"]
            for j in o.get("processes", o["jobs"])
            if (o["kind"] not in motor.AGENT_KINDS and o["kind"] != "Orphans")
            or j.get("role") in ("helper", "host", "unclassified")
        ]
        self.termination.reconcile(observed_jobs)
        self.model.apply(s)
        agent_owners = [
            o
            for o in s["owners"]
            if o["kind"] in motor.AGENT_KINDS or o["kind"] == "Orphans"
        ]
        unknown = sum(o.get("unclassified_count", 0) for o in agent_owners)
        self.role_summary.setText(
            f"{unknown} unclassified processes · open an owner for details"
        )
        self.hive.set_jobs(s["agent_jobs"], self.selected_identity)
        if self.owner_key:
            selected = next(
                (
                    i
                    for i, o in enumerate(self.model.rows)
                    if o["key"] == self.owner_key
                ),
                None,
            )
            if selected is not None:
                self.table.selectRow(selected)
        self.hive_title.setText(f"The swarm · {len(s['agent_jobs'])} local jobs")
        kinds = {j["kind"] for j in s["agent_jobs"]}
        self.legend.setText(
            " · ".join(
                f"{kind}: {sum(j['kind'] == kind for j in s['agent_jobs'])} jobs"
                for kind in sorted(kinds)
            )
        )
        largest = s["agent_jobs"][0] if s["agent_jobs"] else None
        self.advice.setText(
            f"Review {largest['name']} · {memory(largest['mem'])}"
            if largest
            else "No local workload identified · hosts still use RAM"
        )
        if not self.preview:
            self.notifications.on_level(s)
        if self.inspector and self.inspector.isVisible():
            match = next(
                (
                    j
                    for j in observed_jobs
                    if tuple(j["identity"]) == self.selected_identity
                ),
                None,
            )
            if match:
                self.inspector.refresh_job(match)
            else:
                self.inspector.action_status.setText(
                    "This job exited. Confirmation cancelled."
                )
                self.inspector.stop.setEnabled(False)
        if getattr(self, "owner_inspector", None) and self.owner_inspector.isVisible():
            self.owner_inspector.refresh()
        for widget in (self.glance, self.overview, self.resources, self.scope):
            widget.update()
        self.update_footer()

    def find_owner(self, key):
        return (
            next((o for o in self.snap["owners"] if o["key"] == key), None)
            if self.snap
            else None
        )

    def owner_label(self, key):
        o = self.find_owner(key)
        return o["label"] if o else "Owner no longer observed"

    def capacity_owner(self):
        if not self.snap:
            return None
        chosen = self.find_owner(self.owner_key)
        # A helper-only selected swarm must not borrow another swarm's mean.
        if chosen:
            return chosen
        agent_owners = [
            o
            for o in self.snap["owners"]
            if o["kind"] in motor.AGENT_KINDS or o["kind"] == "Orphans"
        ]
        return next(
            (o for o in agent_owners if o.get("capacity") and o["count"]),
            agent_owners[0] if agent_owners else None,
        )

    def no_workload_summary(self, owner):
        if owner:
            if owner["count"]:
                return f"{owner['count']} local jobs · capacity unavailable"
            return (
                f"0 local jobs · {owner.get('helper_count', 0)} helpers · "
                f"{owner.get('unclassified_count', 0)} unclassified"
            )
        return "Local jobs appear when identifiable work starts"

    def capacity(self):
        o = self.capacity_owner()
        return (
            (
                o.get("planning_capacity")
                if self.capacity_basis == "planning"
                else o.get("capacity")
            )
            if o
            else None
        )

    def capacity_size(self):
        cap = self.capacity()
        return cap.get("jobSize") if cap else None

    def basis_label(self):
        return "average" if self.capacity_basis == "mean" else "largest-size estimate"

    def capacity_title(self, cap):
        if self.is_stale():
            return "Capacity paused · reading is stale"
        owner = self.capacity_owner()
        if not self.snap or not owner or not owner["count"]:
            return "No observed job size yet"
        if not cap or not cap.get("valid", True):
            return "Capacity unavailable"
        return f"RAM room for {cap['room']} similar " + (
            "job" if cap["room"] == 1 else "jobs"
        )

    def status_sentence(self):
        if self.is_stale():
            return "Waiting for a fresh sample"
        if not self.snap:
            return "Reading the machine"
        return {
            "GREEN": "Room to work",
            "AMBER": "Hold new work",
            "RED": "Stop launching jobs",
            "UNKNOWN": "Required reading unavailable",
        }.get(self.snap["core"]["level"], "Reading unavailable")

    def is_stale(self):
        return bool(
            self.snap
            and (
                self.last_error
                or time.time() - self.snap["ts"] > max(6, self.interval * 3)
            )
        )

    def trend_line(self):
        if self.is_stale():
            return (
                "Last complete reading "
                + elapsed(time.time() - self.snap["ts"])
                + " ago"
            )
        text = self.summary.get("trend_text", "Learning trend…")
        eta = self.summary.get("eta_s")
        if eta is not None:
            text += (
                " · Red line already crossed"
                if eta <= 0
                else " · Red in ~" + elapsed(eta)
            )
        return text

    def core_line(self):
        if not self.snap:
            return "Core: waiting for a valid reply"
        if not self.snap["core"].get("valid", True):
            return "UNAVAILABLE · " + self.snap["core"].get(
                "reason", "required sensor unavailable"
            )
        cap = self.capacity()
        proved = self.snap.get("proved", False) and (
            cap is None or cap.get("proved", False)
        )
        return (
            "PROVED · Lean " + self.snap.get("core_version", "")
            if proved
            else "FALLBACK · unproved Python replica"
        )

    def update_footer(self):
        core = self.core_line()
        prefix = getattr(self, "source_label", "DEMO") + " · " if self.preview else ""
        if self.is_stale():
            prefix = "STALE · "
        text = (
            prefix + core
            if self.view == "glance"
            else f"Sample: {self.interval} s   Notifications: {'on' if self.notifications.enabled else 'off'}   History: {self.history_status} · 24 h     "
            + prefix
            + core
        )
        if self.last_error:
            text = "Sample error · " + self.last_error
        # The queue is checked four times a second, but an unchanged footer
        # needs no Qt style/layout work. Reapplying the same stylesheet forces
        # a style refresh even when the measurements have not changed.
        if self.footer.text() != text:
            self.footer.setText(text)
            self.footer.setToolTip(text)
        style = f"color:{COL['AMBER'] if self.snap and not self.snap.get('proved') else COL['secondary']};"
        if self.footer.styleSheet() != style:
            self.footer.setStyleSheet(style)

    def inspect_job(self, j):
        self.selected_identity = tuple(j["identity"])
        self.owner_key = j["owner"]
        if not self.inspector:
            self.inspector = Inspector(self)
        self.inspector.set_job(j)
        self.inspector.show()
        self.inspector.raise_()
        self.hive.set_jobs(self.snap["agent_jobs"], self.selected_identity)
        self.glance.update()
        self.overview.update()

    def inspect_largest(self):
        if self.snap and self.snap["agent_jobs"]:
            self.inspect_job(self.snap["agent_jobs"][0])

    def inspect_owner(self, index):
        if not index.isValid() or getattr(self, "owner_dialog_open", False):
            return
        o = self.model.rows[index.row()]
        self.owner_key = o["key"]
        self.glance.update()
        self.overview.update()
        dialog = OwnerInspector(self, o["key"])
        self.owner_inspector = dialog
        self.owner_dialog_open = True
        try:
            dialog.exec()
        finally:
            self.owner_dialog_open = False
            self.owner_inspector = None
            dialog.deleteLater()

    def show_settings(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Swarm Meter settings")
        dialog.resize(540, 540)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        layout.addLayout(form)
        interval = QComboBox()
        interval.addItems(["1 s", "2 s", "4 s"])
        interval.setCurrentIndex([1, 2, 4].index(self.interval))
        form.addRow("Sample every", interval)
        basis = QComboBox()
        basis.addItems(
            [
                "Average job in the selected swarm",
                "Largest current job (at least the average)",
            ]
        )
        basis.setCurrentIndex(self.capacity_basis == "planning")
        form.addRow("Estimate room using", basis)
        notices = QCheckBox("Notify when the level changes")
        notices.setChecked(self.notifications.enabled)
        form.addRow(notices)
        info = QPlainTextEdit()
        info.setReadOnly(True)
        layout.addWidget(info)
        if self.snap:
            s = self.snap
            mem = s["mem"]
            pol = s["policy"]
            gpu = s["gpu"]
            info.setPlainText(
                f"Swarm Meter · {VERSION}\n\nThis machine\nRAM: {memory(mem['total'])}\nGPU: {gpu['name'] if gpu else 'Unavailable'}\nCPU: {motor.cpu_threads()} hardware threads\nSwap: {memory(mem['swap_total'])}\n\n"
                f"Policy supplied to Lean\nAmber below {pol['amberPct']}% available RAM\nRed below {pol['redPct']}% available RAM\nWhy: {pol['why']}\n"
                f"Memory waiting: amber at {pol['amberPSI'] / 10:.1f}%, red at {pol['redPSI'] / 10:.1f}%\n\n"
                "GiB = 1024 MiB. Available RAM includes reclaimable memory.\nCapacity assumes jobs of the displayed size and current available RAM. It does not predict future growth or GPU capacity.\n\n"
                "Local jobs are identified workload processes, not a count of logical agents. Host, helper and unclassified RAM remains in total usage but is excluded from average RAM/job.\n\n"
                + self.core_line()
            )
        layout.addWidget(
            label(
                "The light tells you when to hold new work.\nThe bar shows available RAM and its limits.\nOpen the swarm to see who started each job.",
                caption=True,
            )
        )

        def save():
            self.interval = [1, 2, 4][interval.currentIndex()]
            self.capacity_basis = "planning" if basis.currentIndex() else "mean"
            self.notifications.enabled = notices.isChecked()
            self.persist()
            self.glance.update()
            self.overview.update()
            dialog.accept()

        layout.addWidget(button("Done", save, True))
        dialog.exec()

    def closeEvent(self, event):
        self.stop_event.set()
        self.persist()
        self.termination.close()
        if self.inspector:
            self.inspector.close()
        # No joins on the UI thread: the worker owns cleanup and bounded I/O. It
        # will exit within its current read deadlines, and main joins on shutdown.
        super().closeEvent(event)
