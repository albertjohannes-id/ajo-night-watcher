// Reproducible vector artwork rendered with AppKit. No external assets or dependencies.
import AppKit
let destination = CommandLine.arguments[1]
let size = 1024
let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
NSColor.clear.setFill()
NSRect(x: 0, y: 0, width: size, height: size).fill()
let tile = NSBezierPath(roundedRect: NSRect(x: 64, y: 64, width: 896, height: 896), xRadius: 204, yRadius: 204)
NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow(); shadow.shadowColor = NSColor.black.withAlphaComponent(0.24); shadow.shadowBlurRadius = 24; shadow.shadowOffset = NSSize(width: 0, height: -12); shadow.set()
NSColor(calibratedRed: 0.07, green: 0.10, blue: 0.22, alpha: 1).setFill(); tile.fill()
NSGraphicsContext.restoreGraphicsState()
NSGradient(starting: NSColor(calibratedRed: 0.17, green: 0.22, blue: 0.40, alpha: 1), ending: NSColor(calibratedRed: 0.035, green: 0.055, blue: 0.14, alpha: 1))!.draw(in: tile, angle: -70)
let moon = NSBezierPath()
moon.move(to: NSPoint(x: 590, y: 821))
moon.curve(to: NSPoint(x: 279, y: 402), controlPoint1: NSPoint(x: 270, y: 900), controlPoint2: NSPoint(x: 130, y: 603))
moon.curve(to: NSPoint(x: 624, y: 310), controlPoint1: NSPoint(x: 355, y: 294), controlPoint2: NSPoint(x: 518, y: 266))
moon.curve(to: NSPoint(x: 590, y: 821), controlPoint1: NSPoint(x: 379, y: 421), controlPoint2: NSPoint(x: 347, y: 683))
moon.close()
NSGradient(starting: NSColor(calibratedRed: 1, green: 0.91, blue: 0.62, alpha: 1), ending: NSColor(calibratedRed: 0.98, green: 0.69, blue: 0.29, alpha: 1))!.draw(in: moon, angle: -90)
func star(_ x: CGFloat, _ y: CGFloat, _ r: CGFloat) {
 let p = NSBezierPath();p.move(to: NSPoint(x:x,y:y+r));p.line(to:NSPoint(x:x+r*0.26,y:y+r*0.26));p.line(to:NSPoint(x:x+r,y:y));p.line(to:NSPoint(x:x+r*0.26,y:y-r*0.26));p.line(to:NSPoint(x:x,y:y-r));p.line(to:NSPoint(x:x-r*0.26,y:y-r*0.26));p.line(to:NSPoint(x:x-r,y:y));p.line(to:NSPoint(x:x-r*0.26,y:y+r*0.26));p.close();NSColor(calibratedWhite:0.94,alpha:1).setFill();p.fill()
}
star(749, 751, 48);star(814, 619, 22)
let clockRect = NSRect(x: 539, y: 204, width: 290, height: 290)
NSColor(calibratedRed:0.07,green:0.13,blue:0.22,alpha:1).setFill();NSBezierPath(ovalIn: clockRect.insetBy(dx:-22,dy:-22)).fill()
let ring = NSBezierPath(ovalIn: clockRect);ring.lineWidth=25
NSColor(calibratedRed:0.51,green:0.89,blue:0.84,alpha:1).setStroke();ring.stroke()
let hands=NSBezierPath();hands.move(to:NSPoint(x:684,y:434));hands.line(to:NSPoint(x:684,y:349));hands.line(to:NSPoint(x:743,y:312));hands.lineWidth=25;hands.lineCapStyle = .round;hands.lineJoinStyle = .round;hands.stroke()
NSGraphicsContext.restoreGraphicsState()
try bitmap.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:destination))
