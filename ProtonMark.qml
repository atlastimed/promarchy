import QtQuick
import QtQuick.Shapes

// A Proton-style envelope drawn natively, so it stays crisp in a 16px bar
// slot and takes the theme's colors like every other Omarchy bar icon. The
// flap forms the envelope's top edge ("M" silhouette); `filled` turns the
// outline solid, used while there is unread mail.
// The root is the Shape itself, so tools that measure a Shape's own bounds
// (Omarchy's BarIconContent) see the outline rather than a wrapper's box.
Shape {
  id: root

  property real size: 16
  property color color: "white"
  property bool filled: false

  readonly property real u: size / 24
  readonly property real stroke: Math.max(1.3, size * 0.095)

  implicitWidth: size
  implicitHeight: size
  width: size
  height: size

  // The curve renderer antialiases without a layer, so the mark keeps the
  // sub-pixel position the bar centres it at (a layer snaps to whole pixels).
  preferredRendererType: Shape.CurveRenderer
  antialiasing: true

  ShapePath {
    strokeColor: root.color
    strokeWidth: root.stroke
    fillColor: root.filled ? root.color : "transparent"
    joinStyle: ShapePath.RoundJoin
    capStyle: ShapePath.RoundCap

    startX: 2.5 * root.u
    startY: 6 * root.u
    PathLine { x: 2.5 * root.u; y: 17.5 * root.u }
    PathQuad { x: 5 * root.u; y: 20 * root.u; controlX: 2.5 * root.u; controlY: 20 * root.u }
    PathLine { x: 19 * root.u; y: 20 * root.u }
    PathQuad { x: 21.5 * root.u; y: 17.5 * root.u; controlX: 21.5 * root.u; controlY: 20 * root.u }
    PathLine { x: 21.5 * root.u; y: 6 * root.u }
    PathLine { x: 12 * root.u; y: 13.25 * root.u }
    PathLine { x: 2.5 * root.u; y: 6 * root.u }

  }
}
