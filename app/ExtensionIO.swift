import Foundation

/// Only a redirect on the original extension's origin may receive a follow-up request.
final class ExtensionRedirect: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        let home = task.originalRequest?.url.flatMap { originOf($0.absoluteString) }
        let next = request.url.flatMap { originOf($0.absoluteString) }
        completionHandler(home != nil && home == next ? request : nil)
    }
}

/// Read stdout and stderr while the command runs. Keep the final 4 KiB; a daemon inheriting the pipe need not exit.
final class ExtensionOutput {
    private let pipe = Pipe()
    private let queue = DispatchQueue(label: "localmodels.extension-output")
    private var tail = Data()
    private lazy var reader = DispatchSource.makeReadSource(fileDescriptor: pipe.fileHandleForReading.fileDescriptor, queue: queue)

    private func drain() {
        var buf = [UInt8](repeating: 0, count: 8192)
        for _ in 0..<128 where !reader.isCancelled {
            let n = Darwin.read(pipe.fileHandleForReading.fileDescriptor, &buf, buf.count)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 {
                if n == 0 { reader.cancel() }
                return
            }
            tail.append(contentsOf: buf.prefix(n))
            if tail.count > 4096 { tail.removeFirst(tail.count - 4096) }
        }
    }

    func run(_ process: Process, done: @escaping (Int32, String) -> Void) throws {
        let fd = pipe.fileHandleForReading.fileDescriptor
        guard fcntl(fd, F_SETFL, O_NONBLOCK) != -1 else { throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno)) }
        process.standardOutput = pipe; process.standardError = pipe
        reader.setEventHandler { self.drain() }
        reader.setCancelHandler { self.pipe.fileHandleForReading.closeFile() }
        reader.resume()
        process.terminationHandler = { child in
            self.queue.async {
                self.drain()
                self.reader.cancel()
                done(child.terminationStatus, String(decoding: self.tail, as: UTF8.self))
            }
        }
        do { try process.run() }
        catch { reader.cancel(); throw error }
    }
}
