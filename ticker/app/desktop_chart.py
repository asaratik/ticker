"""A native-painted, palette-aware chart. No web engine or Qt add-ons."""

import math
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from ticker import config as tconfig


class Chart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []
        self.unit = "bpm"
        self.empty = "No readings in this range"
        self.setMinimumHeight(205)
        self.setMouseTracking(True)
        self.setAccessibleName("Health data chart")
        self.setAccessibleDescription(self.empty)

    def set_points(self, points, unit="bpm", empty="No readings in this range"):
        self.points = sorted((float(x), float(y)) for x, y in points
                             if math.isfinite(x) and math.isfinite(y))
        self.unit, self.empty = unit or "", empty
        self.setAccessibleDescription(
            "{} readings, from {} to {} {}. Values available in History.".format(
                len(self.points), min(y for _, y in self.points),
                max(y for _, y in self.points), self.unit) if self.points else empty)
        self.update()

    def bounds(self):
        area = QRectF(55, 20, max(1, self.width() - 73), max(1, self.height() - 57))
        x0, x1 = self.points[0][0], self.points[-1][0]
        if x0 == x1:
            x0, x1 = x0 - 30, x1 + 30
        low, high = min(y for _, y in self.points), max(y for _, y in self.points)
        pad = max(1, (high - low) * .15)
        return area, x0, x1, low - pad, high + pad

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.setPen(palette.text().color())
        if not self.points:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty)
            return
        area, x0, x1, y0, y1 = self.bounds()
        painter.drawText(4, 14, self.unit)
        for i in range(4):
            y = area.top() + area.height() * i / 3
            painter.setPen(QPen(palette.mid().color(), 1))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(palette.text().color())
            label = "{:g}".format(round(y1 - (y1 - y0) * i / 3, 1))
            painter.drawText(QRectF(0, y - 9, 46, 18), Qt.AlignmentFlag.AlignRight, label)
        for i in range(3):
            instant = x0 + (x1 - x0) * i / 2
            stamp = datetime.fromtimestamp(instant, tconfig.local_zone())
            label = stamp.strftime("%b %d" if x1 - x0 > 86400 else "%H:%M")
            x = area.left() + area.width() * i / 2
            label_width = painter.fontMetrics().horizontalAdvance(label) + 4
            left = min(max(area.left(), x - label_width / 2), area.right() - label_width)
            painter.drawText(QRectF(left, area.bottom() + 10, label_width, 20),
                             Qt.AlignmentFlag.AlignLeft, label)
        path = QPainterPath()
        for i, (x, y) in enumerate(self.points):
            point = QPointF(area.left() + (x - x0) / (x1 - x0) * area.width(),
                            area.bottom() - (y - y0) / (y1 - y0) * area.height())
            if i == 0:
                path.moveTo(point)
            else:
                path.lineTo(point)
        painter.setPen(QPen(palette.highlight().color(), 2))
        painter.drawPath(path)
        if len(self.points) == 1:
            painter.setBrush(palette.highlight())
            painter.drawEllipse(path.currentPosition(), 3, 3)

    def mouseMoveEvent(self, event):
        if not self.points:
            return
        area, x0, x1, _, _ = self.bounds()
        instant = x0 + (event.position().x() - area.left()) / area.width() * (x1 - x0)
        x, y = min(self.points, key=lambda point: abs(point[0] - instant))
        label = "{} · {:g} {}".format(
            datetime.fromtimestamp(x, tconfig.local_zone()).strftime("%b %d, %H:%M:%S"),
            y, self.unit)
        QToolTip.showText(event.globalPosition().toPoint(), label, self)
