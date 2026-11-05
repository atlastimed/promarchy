import QtQuick
import qs.Commons

// A bar icon drawn as Omarchy's own are (OpticalGlyph with a painted size):
// the glyph's ink, not its line box, is scaled to the bar's icon size and
// centred on the bar's icon canvas. It lives in the plugin rather than using
// OpticalGlyph directly so the widget also loads on Omarchy 4.0, whose
// OpticalGlyph has no paintedSize and whose Style has no bar.iconSize.
Item {
  id: root

  property string text: ""
  property string fontFamily: fontTokens.family
  property real fontSize: barTokens.iconFont
  property color color: "white"
  readonly property var barTokens: Style.bar
  readonly property var fontTokens: Style.font
  // Omarchy 4.5's bar icon size, or its default where the token is missing.
  property real paintedSize: barTokens.iconSize !== undefined ? barTokens.iconSize : Math.min(barTokens.iconCanvas, 12)

  implicitWidth: barTokens.iconCanvas
  implicitHeight: barTokens.iconCanvas

  readonly property int renderedFontSize: Math.max(1, Math.round(fontSize))
  // Tight bounds come back in whole pixels; measuring a hundred times larger
  // keeps them to the outline (as OpticalGlyph does).
  readonly property int metricsScale: 100
  readonly property rect ink: Qt.rect(metrics.tightBoundingRect.x / metricsScale, metrics.tightBoundingRect.y / metricsScale,
    metrics.tightBoundingRect.width / metricsScale, metrics.tightBoundingRect.height / metricsScale)
  readonly property real tightWidth: Math.max(1, ink.width)
  readonly property real tightHeight: Math.max(1, ink.height)
  readonly property real paintedScale: Math.min(paintedSize, width, height) / Math.max(tightWidth, tightHeight)

  TextMetrics {
    id: metrics
    font.family: root.fontFamily
    font.pixelSize: root.renderedFontSize * root.metricsScale
    text: root.text
  }

  Text {
    textFormat: Text.PlainText
    x: root.width / 2 - (root.ink.x + root.tightWidth / 2) * root.paintedScale
    y: root.height / 2 - (baselineOffset + root.ink.y + root.tightHeight / 2) * root.paintedScale
    text: root.text
    color: root.color
    font.family: root.fontFamily
    font.pixelSize: root.renderedFontSize
    transformOrigin: Item.TopLeft
    scale: root.paintedScale
    // Distance fields, as Omarchy uses for scaled bar icons.
    renderType: Text.QtRendering
  }
}
