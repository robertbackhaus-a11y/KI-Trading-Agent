"""Sektoren page: treemap (area = market value, grouped by sector, colour = daily change) plus the sector and position tables (read-only)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSplitter, QTabWidget, QTableWidgetItem, QToolTip, QVBoxLayout, QWidget

from ..viewmodels import treemap
from ..viewmodels.effective import COMPANY_HEADERS, DEFAULT_SORT_COLUMN, EFFECTIVE_COLUMNS, NUMERIC_COLUMNS, EffectiveVM, ETF_HEADERS as EFFECTIVE_ETF_HEADERS
from ..viewmodels.portfolio import DASH, fmt_eur
from ..viewmodels.sectors import ETF_HEADERS, GUARD_TIP, KIND_SECTOR, POSITION_HEADERS, SECTOR_HEADERS, SectorsVM, fmt_signed_pct
from ._common import LEVEL_COLORS, fill_table, make_table, set_form_rows

SORT_KEY = Qt.ItemDataRole.UserRole + 11
EFFECTIVE_BACKGROUND = "#fff6d6"  # the effective columns stand out; direct and ETF stay plain
PAD, CAPTION = 2.0, 18.0
MIN_LABEL_W, MIN_LABEL_H = 46.0, 30.0


def _text_color(background: QColor) -> QColor:
    return QColor("#111111") if background.lightness() > 150 else QColor("#ffffff")


class SortItem(QTableWidgetItem):
    """A table cell that sorts by value (numbers as numbers, an empty value last) instead of by its text."""

    def __lt__(self, other: QTableWidgetItem) -> bool:  # noqa: D105 - Qt API
        a, b = self.data(SORT_KEY), other.data(SORT_KEY)
        if a is None:
            return False
        if b is None:
            return True
        try:
            return a < b
        except TypeError:
            return str(a) < str(b)


class TreemapWidget(QWidget):
    """Draws ``treemap.layout`` of a :class:`SectorsVM`; no data of its own, no interaction beyond tooltips."""

    def __init__(self) -> None:
        super().__init__()
        self.vm: Optional[SectorsVM] = None
        self.tiles: list[treemap.Tile] = []
        self.setMinimumSize(420, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_vm(self, vm: Optional[SectorsVM]) -> None:
        self.vm = vm
        self.relayout()
        self.update()

    def relayout(self) -> None:
        self.tiles = []
        if self.vm is not None and self.vm.available and self.vm.sectors:
            self.tiles = treemap.layout(self.vm.treemap_groups(), (2.0, 2.0, max(0.0, self.width() - 4.0), max(0.0, self.height() - 4.0)), pad=PAD, caption=CAPTION)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.relayout()
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), self.palette().window())
        if not self.tiles or self.vm is None:
            painter.setPen(self.palette().text().color())
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Keine Daten" if self.vm is None or not self.vm.available else "Keine Positionen")
            return
        font = QFont(self.font())
        small = QFont(font)
        small.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        for group_tile in self.tiles:
            group = self.vm.sectors[int(group_tile.key)]
            gx, gy, gw, gh = group_tile.rect
            outer = QRectF(gx, gy, gw, gh)
            sector = group.kind == KIND_SECTOR
            painter.fillRect(outer, QColor("#e4e8ec" if sector else "#dde3ee" if group.kind == "etf_category" else "#d5d9de"))
            pen = QPen(QColor("#6b7580"), 1, Qt.PenStyle.SolidLine if sector else Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawRect(outer.adjusted(0, 0, -1, -1))
            if group_tile.caption_height:
                painter.setFont(font)
                painter.setPen(QColor("#1c2530"))
                painter.drawText(QRectF(gx + 4, gy + 1, gw - 8, group_tile.caption_height), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 painter.fontMetrics().elidedText(f"{group.title} · {group.weight_pct:.1f} %".replace(".", ","), Qt.TextElideMode.ElideRight, int(gw - 8)))
            for child in group_tile.children:
                position = group.positions[int(child.key)]
                x, y, w, h = child.rect
                box = QRectF(x, y, w, h)
                color = QColor(position.color)
                painter.fillRect(box, color)
                painter.setPen(QPen(QColor("#ffffff"), 1, Qt.PenStyle.DashLine if position.lookthrough else Qt.PenStyle.SolidLine))
                painter.drawRect(box.adjusted(0, 0, -1, -1))
                if w >= MIN_LABEL_W and h >= MIN_LABEL_H:
                    painter.setFont(small)
                    painter.setPen(_text_color(color))
                    lines = [position.label, DASH if position.change_pct is None else fmt_signed_pct(position.change_pct, 1)]
                    if position.lookthrough and h >= 3 * MIN_LABEL_H / 2 + 14:
                        lines.append("ETF-Look-through")
                    painter.drawText(box.adjusted(3, 2, -3, -2), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, "\n".join(lines))
        painter.end()

    def tile_at(self, point: QPointF) -> Optional[str]:
        """The tooltip text of the position (or sector) under ``point``."""
        if self.vm is None:
            return None
        for group_tile in self.tiles:
            gx, gy, gw, gh = group_tile.rect
            if not (gx <= point.x() < gx + gw and gy <= point.y() < gy + gh):
                continue
            group = self.vm.sectors[int(group_tile.key)]
            for child in group_tile.children:
                x, y, w, h = child.rect
                if x <= point.x() < x + w and y <= point.y() < y + h:
                    return group.positions[int(child.key)].tip
            return (f"{group.title}\n{group.direct_count} direkt, {group.etf_count} ETF; {group.weight_pct:.1f} % des investierten Werts "
                    f"(direkt {fmt_eur(group.direct_value)}, ETF-Look-through {fmt_eur(group.etf_value)})").replace(".", ",", 1)
        return None

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        text = self.tile_at(event.position())
        if text:
            QToolTip.showText(event.globalPosition().toPoint(), text, self)
        else:
            QToolTip.hideText()


class SectorsView(QWidget):
    refresh_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("<h2>Sektoren</h2>"))
        top.addStretch(1)
        self.refreshed = QLabel("Letzte Aktualisierung: " + DASH)
        self.button = QPushButton("Aktualisieren")
        self.button.setToolTip("Lesende Abfrage: get_sector_overview")
        self.button.clicked.connect(self.refresh_requested.emit)
        top.addWidget(self.refreshed)
        top.addWidget(self.button)
        outer.addLayout(top)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        outer.addWidget(self.message)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        page = QWidget()  # tab 1: the sectors
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 4, 0, 0)
        self.summary = QHBoxLayout()
        page_layout.addLayout(self.summary)
        self.treemap = TreemapWidget()
        box = QGroupBox("Exposure nach Sektoren (Fläche = Marktwert, Farbe = Tagesperformance, grau = keine Daten, gestrichelter Rand = Anteil aus ETF-Look-through)")
        inner = QVBoxLayout(box)
        inner.addWidget(self.treemap)
        legend = QLabel("<span style='color:#c0392b'>■</span> −3 % und tiefer &nbsp; <span style='color:#7a828a'>■</span> 0 % &nbsp; <span style='color:#1f8c4c'>■</span> +3 % und höher &nbsp; "
                        "<span style='color:#b4bcc6'>■</span> keine Tagesperformance")
        inner.addWidget(legend)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(box)
        lower = QWidget()
        row = QHBoxLayout(lower)
        row.setContentsMargins(0, 0, 0, 0)
        self.sector_table = make_table(SECTOR_HEADERS)
        self.position_table = make_table(POSITION_HEADERS)
        self.etf_table = make_table(ETF_HEADERS)
        self.etf_table.setMaximumHeight(130)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        for caption, table in (("Sektoren (effektive Exposure: direkt + ETF-Look-through)", self.sector_table), ("ETF-Look-through", self.etf_table)):
            group = QGroupBox(caption)
            layout = QVBoxLayout(group)
            layout.addWidget(table)
            left_layout.addWidget(group, 3 if table is self.sector_table else 1)
        row.addWidget(left, 3)
        group = QGroupBox("Positionen und ETF-Beiträge")
        layout = QVBoxLayout(group)
        layout.addWidget(self.position_table)
        row.addWidget(group, 2)
        split.addWidget(lower)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        page_layout.addWidget(split, 1)
        self.notes = QLabel("")
        self.notes.setWordWrap(True)
        page_layout.addWidget(self.notes)
        self.tabs.addTab(page, "Sektoren")
        self._build_effective_tab()

    def _build_effective_tab(self) -> None:
        """Tab 2: "Einzelwertrisiko / Effective Exposure" -- the largest companies with direct and ETF share separated, and the ETF holdings coverage."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        self.effective_message = QLabel("")
        self.effective_message.setWordWrap(True)
        layout.addWidget(self.effective_message)
        frame = QFrame()  # the compact headline: the biggest effective single position and the key counts
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_layout = QVBoxLayout(frame)
        self.effective_headline = QLabel("")
        self.effective_headline.setTextFormat(Qt.TextFormat.RichText)
        frame_layout.addWidget(self.effective_headline)
        self.effective_chips = QHBoxLayout()
        frame_layout.addLayout(self.effective_chips)
        layout.addWidget(frame)
        self.effective_summary = QHBoxLayout()
        layout.addLayout(self.effective_summary)
        self._company_sort = (DEFAULT_SORT_COLUMN, Qt.SortOrder.DescendingOrder)  # Effektiv %, largest first, until the user sorts otherwise
        self.company_table = make_table(COMPANY_HEADERS)
        self.effective_etf_table = make_table(EFFECTIVE_ETF_HEADERS)
        self.effective_etf_table.setMaximumHeight(140)
        split = QSplitter(Qt.Orientation.Vertical)
        for caption, table in (("Größte effektive Firmen-Exposure (direkt + Anteil aus den gehaltenen ETFs; eine Firma zählt einmal)", self.company_table),
                               ("ETF-Holdings: Abdeckung und Verknüpfung", self.effective_etf_table)):
            box = QGroupBox(caption)
            inner = QVBoxLayout(box)
            inner.addWidget(table)
            split.addWidget(box)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        self.company_table.setSortingEnabled(False)
        self.company_table.horizontalHeader().sortIndicatorChanged.connect(self._on_company_sort)
        layout.addWidget(split, 1)
        self.effective_notes = QLabel("")
        self.effective_notes.setWordWrap(True)
        layout.addWidget(self.effective_notes)
        self.tabs.addTab(page, "Einzelwertrisiko")

    def _on_company_sort(self, column: int, order) -> None:
        self._company_sort = (column, order)

    def _fill_companies(self, vm: EffectiveVM) -> None:
        """The company table: numbers sort as numbers, the effective columns are highlighted, "—" where there is no direct position or ETF share."""
        table = self.company_table
        header = table.horizontalHeader()
        header.blockSignals(True)  # re-sorting after a refresh must not count as a choice of the user
        table.setSortingEnabled(False)
        table.setColumnCount(len(COMPANY_HEADERS))
        table.setHorizontalHeaderLabels(COMPANY_HEADERS)
        table.setRowCount(len(vm.company_rows))
        background = QBrush(QColor(EFFECTIVE_BACKGROUND))
        for r, (row, company) in enumerate(zip(vm.company_rows, vm.companies)):
            level = vm.company_levels[r] if r < len(vm.company_levels) else ""
            for c, text in enumerate(row):
                item = SortItem(text)
                item.setData(SORT_KEY, vm.company_keys[r][c] if r < len(vm.company_keys) else text)
                item.setToolTip(company.tip)
                if c in NUMERIC_COLUMNS:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if level in LEVEL_COLORS:
                    item.setForeground(QBrush(QColor(LEVEL_COLORS[level])))
                if c in EFFECTIVE_COLUMNS:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setBackground(background)
                table.setItem(r, c, item)
        table.setSortingEnabled(True)
        table.sortItems(*self._company_sort)
        table.resizeColumnsToContents()
        header.blockSignals(False)

    def show_effective_loading(self) -> None:
        self.effective_message.setText("Lade Daten …")

    def show_effective(self, vm: EffectiveVM) -> None:
        if not vm.available:
            self.effective_message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>Keine Daten: {vm.error}</span>")
            fill_table(self.company_table, COMPANY_HEADERS, [])
            fill_table(self.effective_etf_table, EFFECTIVE_ETF_HEADERS, [])
            self.effective_headline.setText("")
            return
        self.effective_message.setText("")
        self.effective_headline.setText(f"<span style='font-size:11pt'>Größte effektive Einzelposition:</span> <b style='font-size:15pt'>{vm.headline}</b>"
                                        f"<br><span style='font-size:10pt'>{vm.headline_detail}</span>")
        while self.effective_chips.count():
            old = self.effective_chips.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        for caption, value, level in vm.chips:
            color = LEVEL_COLORS.get(level, "")
            chip = QLabel(f"<small>{caption}</small><br><b{(' style=' + chr(39) + 'color:' + color + chr(39)) if color else ''}>{value}</b>")
            chip.setToolTip("Holdings, auf denen die ETF-Anteile beruhen" if caption == "Holdings-Daten" else "Firmen mit effektivem Anteil am investierten Wert über der Schwelle (nur Anzeige, kein Limit)")
            self.effective_chips.addWidget(chip)
        self.effective_chips.addStretch(1)
        while self.effective_summary.count():
            old = self.effective_summary.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        for caption, value in vm.summary:
            self.effective_summary.addWidget(QLabel(f"<small>{caption}</small><br><b>{value}</b>"))
        self.effective_summary.addStretch(1)
        self._fill_companies(vm)
        fill_table(self.effective_etf_table, EFFECTIVE_ETF_HEADERS, vm.etf_rows, vm.etf_levels)
        for r, tip in enumerate(vm.etf_tips):
            for c in range(len(EFFECTIVE_ETF_HEADERS)):
                item = self.effective_etf_table.item(r, c)
                if item is not None:
                    item.setToolTip(tip)
        self.effective_notes.setText("<small>" + "<br>".join(vm.notes) + "</small>")

    def show_loading(self) -> None:
        self.button.setEnabled(False)
        self.message.setText("Lade Daten …")

    def show_vm(self, vm: SectorsVM) -> None:
        self.button.setEnabled(True)
        self.refreshed.setText("Letzte Aktualisierung: " + vm.refreshed)
        if not vm.available:
            self.message.setText(f"<span style='color:{LEVEL_COLORS['blocker']}'>Keine Daten: {vm.error}</span>")
            self.treemap.set_vm(None)
            fill_table(self.sector_table, SECTOR_HEADERS, [])
            fill_table(self.position_table, POSITION_HEADERS, [])
            fill_table(self.etf_table, ETF_HEADERS, [])
            return
        self.message.setText("")
        while self.summary.count():
            old = self.summary.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        for caption, value in vm.summary:
            label = QLabel(f"<small>{caption}</small><br><b>{value}</b>")
            self.summary.addWidget(label)
        self.summary.addStretch(1)
        self.treemap.set_vm(vm)
        fill_table(self.sector_table, SECTOR_HEADERS, vm.sector_rows, vm.sector_levels)
        for r, group in enumerate(vm.sectors):
            item = self.sector_table.item(r, len(SECTOR_HEADERS) - 1)
            if item is not None:
                item.setToolTip(GUARD_TIP.get(group.guard, ""))
        fill_table(self.position_table, POSITION_HEADERS, vm.position_rows)
        fill_table(self.etf_table, ETF_HEADERS, vm.etf_rows, vm.etf_levels)
        self.notes.setText("<small>" + "<br>".join(vm.notes) + "</small>")
