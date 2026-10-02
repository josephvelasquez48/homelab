// cam-capture: a camera's frames into ffmpeg, in the mode we asked for.
//
//   cam-capture <camera name> <width> <height> <fps> -- <ffmpeg command...>
//
// Exists because ffmpeg can't pick the C922's mode itself. Its avfoundation
// input applies the LAST format matching the size, with a frame rate taken
// from whichever format matched first; on the C922 that's an uncompressed
// format the camera can only run at 5-10 fps over USB 2, the frame rate
// throws, and ffmpeg logs "Configuration of video device failed, falling
// back to default" and carries on at 5 fps. This picks a format that really
// runs at the requested rate (the camera's MJPEG one, which AVFoundation
// decodes), and writes raw NV12 frames to ffmpeg's stdin for it to encode.
//
// It starts ffmpeg itself so that the two live and die together: stopping
// this (MediaMTX does, when the last viewer leaves) SIGKILLs ffmpeg, and if
// ffmpeg exits, the next write fails and this exits too. ffmpeg capturing
// directly ignored MediaMTX's stop and kept the camera, which then left
// every later start falling back to the default mode.
//
// Built by install.py with the Command Line Tools' swiftc.

import AVFoundation
import Foundation

func log(_ message: String) {
    FileHandle.standardError.write(("cam-capture: " + message + "\n").data(using: .utf8)!)
}

let args = CommandLine.arguments
guard let split = args.firstIndex(of: "--"), split == 5, args.count > 6,
      let width = Int32(args[2]), let height = Int32(args[3]), let fps = Double(args[4]) else {
    log("usage: cam-capture <camera name> <width> <height> <fps> -- <ffmpeg command...>")
    exit(2)
}
let cameraName = args[1]

let devices = AVCaptureDevice.DiscoverySession(
    deviceTypes: [.external], mediaType: .video, position: .unspecified).devices
guard let device = devices.first(where: { $0.localizedName == cameraName }) else {
    log("no camera named \"\(cameraName)\"; found: \(devices.map(\.localizedName))")
    exit(1)
}

// Formats at this size that really reach this rate, compressed ones first.
let matching = device.formats.filter { format in
    let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
    return size.width == width && size.height == height &&
        format.videoSupportedFrameRateRanges.contains { abs($0.maxFrameRate - fps) < 0.01 }
}
let format = matching.first {
    CMFormatDescriptionGetMediaSubType($0.formatDescription) == kCMVideoCodecType_JPEG_OpenDML
} ?? matching.first
guard let format,
      let range = format.videoSupportedFrameRateRanges.first(where: { abs($0.maxFrameRate - fps) < 0.01 }) else {
    log("\(cameraName) has no \(width)x\(height) mode at \(fps) fps")
    exit(1)
}

// ffmpeg, reading frames from our stdout-to-its-stdin pipe.
let ffmpeg = Process()
ffmpeg.executableURL = URL(fileURLWithPath: args[split + 1])
ffmpeg.arguments = Array(args[(split + 2)...])
let pipe = Pipe()
ffmpeg.standardInput = pipe
ffmpeg.terminationHandler = { process in
    log("ffmpeg exited (\(process.terminationStatus))")
    exit(1)
}

func stop(_ status: Int32) -> Never {
    if ffmpeg.isRunning { kill(ffmpeg.processIdentifier, SIGKILL) }
    exit(status)
}

// A closed pipe should end us through the write error, not kill us mid-frame.
signal(SIGPIPE, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGINT, SIGTERM] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { stop(0) }
    source.resume()
    signalSources.append(source)
}

final class FrameWriter: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let out: Int32
    var lastFrame = Date()

    init(fd: Int32) { out = fd }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer,
                       from connection: AVCaptureConnection) {
        guard let image = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        CVPixelBufferLockBaseAddress(image, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(image, .readOnly) }
        // NV12 is two planes, luma then interleaved chroma at half height.
        // Rows can be padded past the picture width, so copy row by row.
        for plane in 0..<CVPixelBufferGetPlaneCount(image) {
            guard let base = CVPixelBufferGetBaseAddressOfPlane(image, plane) else { return }
            let stride = CVPixelBufferGetBytesPerRowOfPlane(image, plane)
            let rowBytes = CVPixelBufferGetWidthOfPlane(image, plane) * (plane == 0 ? 1 : 2)
            for row in 0..<CVPixelBufferGetHeightOfPlane(image, plane) {
                if !writeAll(base + row * stride, rowBytes) {
                    log("ffmpeg stopped reading")
                    stop(1)
                }
            }
        }
        lastFrame = Date()
    }

    func writeAll(_ pointer: UnsafeRawPointer, _ count: Int) -> Bool {
        var done = 0
        while done < count {
            let n = write(out, pointer + done, count - done)
            if n <= 0 { return false }
            done += n
        }
        return true
    }
}

let session = AVCaptureSession()
let output = AVCaptureVideoDataOutput()
output.videoSettings = [
    kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
]
output.alwaysDiscardsLateVideoFrames = true
let writer = FrameWriter(fd: pipe.fileHandleForWriting.fileDescriptor)
output.setSampleBufferDelegate(writer, queue: DispatchQueue(label: "frames"))

do {
    let input = try AVCaptureDeviceInput(device: device)
    session.beginConfiguration()
    guard session.canAddInput(input), session.canAddOutput(output) else {
        log("can't attach \(cameraName) to a capture session")
        exit(1)
    }
    session.addInput(input)
    session.addOutput(output)
    session.commitConfiguration()

    // After the input is attached, or the session's preset resets it; and
    // held through startRunning, which can otherwise do the same.
    try device.lockForConfiguration()
    device.activeFormat = format
    device.activeVideoMinFrameDuration = range.minFrameDuration
    device.activeVideoMaxFrameDuration = range.minFrameDuration
    try ffmpeg.run()
    session.startRunning()
    device.unlockForConfiguration()
} catch {
    log("\(error)")
    stop(1)
}
log("\(cameraName): \(width)x\(height) at \(fps) fps, " +
    "format \(FourCharCode(CMFormatDescriptionGetMediaSubType(format.formatDescription)).string)")

// The camera unplugged or wedged: exit, and MediaMTX starts us again.
NotificationCenter.default.addObserver(
    forName: .AVCaptureDeviceWasDisconnected, object: device, queue: .main) { _ in
    log("\(cameraName) disconnected")
    stop(1)
}
Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in
    if Date().timeIntervalSince(writer.lastFrame) > 5 {
        log("no frames for 5 s")
        stop(1)
    }
}

RunLoop.main.run()

extension FourCharCode {
    var string: String {
        String([24, 16, 8, 0].map { Character(UnicodeScalar(UInt8((self >> $0) & 0xff))) })
    }
}
