import QtQuick
import qs.Commons

// A bar label on the bar's icon canvas, centred on its figures rather than its
// line box, as WidgetButton and BarIconButton centre theirs, so it lines up
// with the icon beside it.
Item {
  id: root

  property string text: ""
  property color color: "white"
  property string fontFamily: Style.font.family
  property int pixelSize: Style.font.body

  implicitWidth: label.implicitWidth
  implicitHeight: Style.bar.iconCanvas
  width: implicitWidth
  height: implicitHeight

  Text {
    id: label
    textFormat: Text.PlainText
    text: root.text
    color: root.color
    font.family: root.fontFamily
    font.pixelSize: root.pixelSize
    renderType: Text.NativeRendering
    y: (root.height - height) / 2 + (metrics.descent - metrics.ascent + metrics.tightBoundingRect("0").height) / 2
  }

  FontMetrics { id: metrics; font: label.font }
}
