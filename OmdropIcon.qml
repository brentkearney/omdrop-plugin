import QtQuick
import QtQuick.Shapes
import qs.Commons

// A parachute carrying a box. Nothing in a Nerd Font draws this, and an SVG
// asset would not follow the bar's foreground colour, so it is a Shape:
// authored on a 24 grid and scaled to whatever the caller asks for, which
// keeps the stroke proportional at every size. Five strokes -- dome, the
// chord that closes it, two rigging lines, and the payload.
Item {
  id: root

  property color color: Color.foreground
  property real iconSize: Style.font.icon
  property bool crossed: false

  // Drifting, while the radio gate runs. A parachute already implies descent,
  // so it sways and sinks rather than spinning: the wait is 15-60s and a
  // spinner that long reads as a hang, where drift reads as travel.
  property bool drifting: false

  implicitWidth: iconSize
  implicitHeight: iconSize

  Item {
    anchors.centerIn: parent
    width: 24
    height: 24
    scale: root.iconSize / 24

    // verticalCenterOffset, not y: the item is anchored with centerIn, and an
    // animation on y silently loses to the anchor. Two loops of different
    // length, so they never resolve together and it reads as drift.
    SequentialAnimation on anchors.verticalCenterOffset {
      running: root.drifting
      loops: Animation.Infinite
      alwaysRunToEnd: true
      NumberAnimation { to: 1.6; duration: 1500; easing.type: Easing.InOutSine }
      NumberAnimation { to: -1.6; duration: 1500; easing.type: Easing.InOutSine }
    }

    SequentialAnimation on rotation {
      running: root.drifting
      loops: Animation.Infinite
      alwaysRunToEnd: true
      NumberAnimation { to: 4; duration: 1900; easing.type: Easing.InOutSine }
      NumberAnimation { to: -4; duration: 1900; easing.type: Easing.InOutSine }
    }

    Shape {
      anchors.fill: parent

      ShapePath {
        strokeColor: root.color
        fillColor: "transparent"
        strokeWidth: 1.7
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        PathSvg { path: "M3.2 11a8.8 8.8 0 0 1 17.6 0" }
        PathSvg { path: "M3.2 11h17.6" }
        PathSvg { path: "M3.2 11 9 16" }
        PathSvg { path: "M20.8 11 15 16" }
        PathSvg { path: "M9 16h6v6h-6z" }
      }

      // Struck through while the radio support is missing. Kept as its own
      // path with a real `d` at all times -- an empty path string takes the
      // whole Shape down with it.
      ShapePath {
        strokeColor: root.crossed ? root.color : "transparent"
        fillColor: "transparent"
        strokeWidth: 1.7
        capStyle: ShapePath.RoundCap
        PathSvg { path: "M3 21 21 3" }
      }
    }
  }
}
