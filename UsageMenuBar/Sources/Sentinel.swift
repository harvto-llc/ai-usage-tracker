import Foundation
import SweetCookieKit

@MainActor
class Sentinel {
    static let shared = Sentinel()
    private let cookieClient = BrowserCookieClient()
    private let apiURL = URL(string: "http://127.0.0.1:8000/sentinel/report")!
    private var lastReportHash: String = ""

    func start() {
        // Run every 15 minutes
        Timer.scheduledTimer(withTimeInterval: 900, repeats: true) { _ in
            Task {
                await self.scanAndReport()
            }
        }
        // Initial scan
        Task {
            await self.scanAndReport()
        }
    }

    func scanAndReport(force: Bool = false) async {
        var reports: [String: String] = [:]

        // Claude
        if let claude = await cookieHeader(
            domains: ["claude.ai"],
            requiredNames: ["sessionKey", "lastActiveOrg"]
        ) {
            reports["claude"] = claude
        }

        // Codex / ChatGPT
        if let codex = await cookieHeader(
            domains: ["chatgpt.com"],
            requiredNames: ["__Secure-next-auth.session-token"]
        ) {
            reports["codex"] = codex
        }

        // Cursor
        if let cursor = await cookieHeader(
            domains: ["cursor.com", "cursor.sh"],
            requiredNames: ["WorkosCursorSessionToken"]
        ) {
            reports["cursor"] = cursor
        }

        guard !reports.isEmpty else { return }

        let currentHash = reports.description.hashValue.description
        if !force, currentHash == lastReportHash { return }

        do {
            var request = URLRequest(url: apiURL)
            request.httpMethod = "POST"
            request.addValue("application/json", forHTTPHeaderField: "Content-Type")
            
            if let secret = apiSecret() {
                request.addValue("Bearer \(secret)", forHTTPHeaderField: "Authorization")
            }

            request.httpBody = try JSONSerialization.data(withJSONObject: ["cookies": reports])
            
            let (_, response) = try await URLSession.shared.data(for: request)
            if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                self.lastReportHash = currentHash
                print("Sentinel: Successfully reported cookies to API")
            }
        } catch {
            print("Sentinel: Failed to report cookies: \(error)")
        }
    }

    private func cookieHeader(domains: [String], requiredNames: [String]) async -> String? {
        let query = BrowserCookieQuery(domains: domains)
        for browser in [Browser.chrome, Browser.arc] {
            do {
                let sources = try cookieClient.records(matching: query, in: browser)
                var cookies: [String: String] = [:]
                for source in sources {
                    for record in source.records where cookies[record.name] == nil {
                        cookies[record.name] = record.value
                    }
                }
                guard requiredNames.allSatisfy({ requiredName in
                    cookies.keys.contains(where: {
                        $0 == requiredName || $0.hasPrefix(requiredName + ".")
                    })
                }) else { continue }
                let selectedNames = cookies.keys.filter { name in
                    requiredNames.contains(where: {
                        name == $0 || name.hasPrefix($0 + ".")
                    })
                }
                return selectedNames.sorted().compactMap { name in
                    cookies[name].map { "\(name)=\($0)" }
                }.joined(separator: "; ")
            } catch {
                continue
            }
        }
        return nil
    }

    private func apiSecret() -> String? {
        if let secret = ProcessInfo.processInfo.environment["USAGE_TRACKER_SECRET"], !secret.isEmpty {
            return secret
        }
        let config = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".usage-tracker/config")
        guard let contents = try? String(contentsOf: config, encoding: .utf8) else { return nil }
        for line in contents.split(separator: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("USAGE_TRACKER_SECRET=") {
                return String(trimmed.dropFirst("USAGE_TRACKER_SECRET=".count))
            }
        }
        return nil
    }
}
