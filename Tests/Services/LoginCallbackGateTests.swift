import XCTest
@testable import QuotaBar

/// LoginCallbackGate 状态机 (R-6): onComplete / onCancel 只允许第一次生效,
/// 之后到达的回调一律丢弃 — 迟到的完成回调不得在取消之后写凭据.
/// 纯值语义, 无 IO / 无 UI / 无并发.
final class LoginCallbackGateTests: XCTestCase {

    func testFreshGateAllowsBothCallbacks() {
        let gate = LoginCallbackGate()
        XCTAssertFalse(gate.didComplete)
        XCTAssertFalse(gate.didCancel)
        XCTAssertTrue(gate.allowsCompletion())
        XCTAssertTrue(gate.allowsCancellation())
    }

    func testAllowsFirstCompletionOnly() {
        var gate = LoginCallbackGate()
        gate.markCompleted()
        XCTAssertTrue(gate.didComplete)
        XCTAssertFalse(gate.allowsCompletion(), "完成后不得再次生效 (连点「完成并提取」)")
    }

    func testAllowsFirstCancellationOnly() {
        var gate = LoginCallbackGate()
        gate.markCancelled()
        XCTAssertTrue(gate.didCancel)
        XCTAssertFalse(gate.allowsCancellation(), "取消后不得再次生效")
    }

    func testCompletionIgnoredAfterCancel() {
        // R-6 核心路径: 用户取消 → getAllCookies 迟到的完成回调必须被丢弃,
        // 否则取消之后凭据仍被改写.
        var gate = LoginCallbackGate()
        gate.markCancelled()
        XCTAssertFalse(gate.allowsCompletion(), "取消后到达的完成回调不得生效")
    }

    func testCancelIgnoredAfterCompletion() {
        // 原 didComplete 语义: onComplete 已回调 → 关窗不再触发 onCancel,
        // 避免 onComplete 后又报一次取消.
        var gate = LoginCallbackGate()
        gate.markCompleted()
        XCTAssertFalse(gate.allowsCancellation(), "完成后的关窗不得再报取消")
    }

    func testRepeatedMarksAreIdempotent() {
        var gate = LoginCallbackGate()
        gate.markCancelled()
        gate.markCancelled()
        gate.markCompleted()   // 顺序错乱也保持终态: 任一置位后另一通道即封闭
        XCTAssertTrue(gate.didCancel)
        XCTAssertTrue(gate.didComplete)
        XCTAssertFalse(gate.allowsCompletion())
        XCTAssertFalse(gate.allowsCancellation())
    }
}
