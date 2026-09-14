import Foundation

/// Utility functions for parsing and formatting reset times in Swift.
struct ResetTimeFormatter {

    /// Parse a reset time string and return a Date object.
    /// Handles formats like:
    /// - "Mar 14 2:30 PM" (from Python _format_reset_time)
    /// - ISO 8601 timestamps
    ///
    /// - Parameter resetStr: Reset time string, or nil
    /// - Returns: Date object in local timezone, or nil if parsing fails
    static func parseResetTime(_ resetStr: String?) -> Date? {
        guard let resetStr = resetStr, !resetStr.isEmpty else { return nil }

        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "en_US_POSIX")
        dateFormatter.timeZone = TimeZone.current

        // Try parsing formatted strings like "Mar 14 2:30 PM"
        dateFormatter.dateFormat = "MMM d h:mm a"
        if var date = dateFormatter.date(from: resetStr) {
            let now = Date()
            let calendar = Calendar.current

            // If the parsed date is in the past, assume it's tomorrow
            if date < now {
                date = calendar.date(byAdding: .day, value: 1, to: date) ?? date
            }
            return date
        }

        // Try ISO 8601
        if let date = ISO8601DateFormatter().date(from: resetStr) {
            return date
        }

        return nil
    }

    /// Compute the duration remaining until reset.
    ///
    /// - Parameter resetStr: Reset time string from API (e.g., "Mar 14 2:30 PM")
    /// - Returns: Dictionary with keys: 'days', 'hours', 'minutes', 'formatted'
    ///   Or nil if parsing fails or reset is in the past
    static func computeDurationToReset(_ resetStr: String?) -> [String: Any]? {
        guard let resetDate = parseResetTime(resetStr) else { return nil }

        let now = Date()
        guard resetDate > now else { return nil }

        let delta = resetDate.timeIntervalSince(now)
        let totalSeconds = Int(delta)

        let days = totalSeconds / 86400
        let remaining = totalSeconds % 86400
        let hours = remaining / 3600
        let minutes = (remaining % 3600) / 60

        // Format
        let formatted: String
        if days > 0 {
            formatted = "\(days)d \(hours)h \(minutes)m"
        } else if hours > 0 {
            formatted = "\(hours)h \(minutes)m"
        } else {
            formatted = "\(minutes)m"
        }

        return [
            "days": days,
            "hours": hours,
            "minutes": minutes,
            "total_seconds": totalSeconds,
            "formatted": formatted,
        ]
    }

    /// Format reset time for display as "Resets 2:30 PM (2h 30m)".
    ///
    /// - Parameter resetStr: Reset time string from API
    /// - Returns: Formatted string like "Resets 2:30 PM (2h 30m)" or nil
    static func formatResetDisplay(_ resetStr: String?) -> String? {
        guard let resetDate = parseResetTime(resetStr) else { return nil }
        guard let duration = computeDurationToReset(resetStr),
              let formattedDuration = duration["formatted"] as? String else { return nil }

        let dateFormatter = DateFormatter()
        dateFormatter.timeStyle = .short
        dateFormatter.timeZone = TimeZone.current
        let timeStr = dateFormatter.string(from: resetDate)

        return "Resets \(timeStr) (\(formattedDuration))"
    }
}
