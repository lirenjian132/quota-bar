import Foundation
@testable import QuotaBar

class MockNetworkService: NetworkService {
    var mockData: Data?
    var mockResponse: URLResponse?
    var mockError: Error?
    var lastRequest: URLRequest?
    // 多次请求场景 (如 TokenRhythm 先查 summary 再查 expiring-credits):
    // 非空时按顺序逐个弹出, 耗尽或为空时回落到单值 mockData/mockResponse.
    var responseSequence: [(Data, HTTPURLResponse)] = []
    private var sequenceIndex = 0

    func data(from request: URLRequest) async throws -> (Data, URLResponse) {
        lastRequest = request
        if let error = mockError {
            throw error
        }
        if sequenceIndex < responseSequence.count {
            let item = responseSequence[sequenceIndex]
            sequenceIndex += 1
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
