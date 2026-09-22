import Foundation
@testable import QuotaBar

class MockNetworkService: NetworkService {
    var mockData: Data?
    var mockResponse: URLResponse?
    var mockError: Error?
    var lastRequest: URLRequest?
    // 请求计数: 断言缓存命中/清除时是否真的发了请求 (不依赖 lastRequest 的时序).
    var requestCount = 0
    // 多次请求场景 (如 TokenRhythm 先查 summary 再查 expiring-credits):
    // 非空时按顺序逐个弹出, 耗尽或为空时回落到单值 mockData/mockResponse.
    // 重新赋值时重置游标 (测试中途换序列是常见写法; 不重置会从旧偏移继续弹, 拿到错位响应).
    var responseSequence: [(Data, HTTPURLResponse)] = [] { didSet { sequenceIndex = 0 } }
    // 顺序响应模式下按序捕获每次请求, 供断言各次请求的 URL/头部.
    // 默认空数组而非 optional: 调用方无需先置 [] 再断言 (P2-10).
    var sequenceCapture: [URLRequest] = []
    private var sequenceIndex = 0
    // 顺序响应是可变状态; 加锁保证被并发 task 共享时不会竞态.
    private let lock = NSLock()

    func data(from request: URLRequest) async throws -> (Data, URLResponse) {
        // lastRequest/计数必须记在 mockError 检查之前: 传输失败也要留下
        // 请求现场供断言 (曾经被挪到 error 检查之后, mockError 时不再记录).
        lock.lock()
        lastRequest = request
        requestCount += 1
        if let error = mockError {
            lock.unlock()
            throw error
        }
        defer { lock.unlock() }
        if sequenceIndex < responseSequence.count {
            let item = responseSequence[sequenceIndex]
            sequenceIndex += 1
            sequenceCapture.append(request)
            return item
        }
        guard let data = mockData, let response = mockResponse else {
            throw URLError(.badServerResponse)
        }
        return (data, response)
    }

    static func makeResponse(url: String, statusCode: Int) -> HTTPURLResponse {
        HTTPURLResponse(url: URL(string: url)!, statusCode: statusCode, httpVersion: nil, headerFields: nil)!
    }
}
