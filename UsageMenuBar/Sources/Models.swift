import Foundation

struct UsageStats: Codable {
    let timestamp: Int?
    let extra: Double
    let extraReset: String?
    let extraSpentUsd: Double?
    let extraLimitUsd: Double?
    let extraBalanceUsd: Double?
    let riskOutlook: String?
    let burn: Double?
    let workload: String?
    let codexBurn: Double?
    let lockEta: Double?
    let outputDensity: Double?
    let cacheHealthPct: Double?
    let streak: Int?
    let weeklyPace: WeeklyPace?
    let claudeToday: ClaudeToday?
    let codexToday: CodexToday?
    let claudeTotals: ClaudeTotals?
    let codexTotals: CodexTotals?
    let claudeQuota: ClaudeQuota?
    let codexQuota: CodexQuota?
    let codexAnalyticsSummary: CodexAnalyticsSummary?
    let cursor: CursorStats?
    let providerRegistry: [ProviderRegistryEntry]?
    let providersLatest: [String: ProviderLatestSnapshot]?
    let providerPeriods: [String: ProviderPeriods]?
    let providerHealth: [String: ProviderHealth]?
    let providerTrends: [String: ProviderUsageTrend]?
    let gapRollups: GapRollupsPayload?

    enum CodingKeys: String, CodingKey {
        case timestamp, extra, burn, workload, streak, cursor
        case riskOutlook = "risk_outlook"
        case codexBurn = "codex_burn"
        case lockEta = "lock_eta"
        case outputDensity = "output_density"
        case cacheHealthPct = "cache_health"
        case weeklyPace = "weekly_pace"
        case extraReset = "extra_reset"
        case extraSpentUsd = "extra_spent_usd"
        case extraLimitUsd = "extra_limit_usd"
        case extraBalanceUsd = "extra_balance_usd"
        case claudeToday = "claude_today"
        case codexToday = "codex_today"
        case claudeTotals = "claude_totals"
        case codexTotals = "codex_totals"
        case claudeQuota = "claude_quota"
        case codexQuota = "codex_quota"
        case codexAnalyticsSummary = "codex_analytics_summary"
        case providerRegistry = "provider_registry"
        case providersLatest = "providers_latest"
        case providerPeriods = "provider_periods"
        case providerHealth = "provider_health"
        case providerTrends = "provider_trends"
        case gapRollups = "gap_rollups"
    }
}

struct ProviderUsageTrend: Codable {
    let days: Int
    let points: [ProviderTrendPoint]
    let last7dTokens: Int?
    let previous7dTokens: Int?
    let changePct: Double?
    let topModel7d: TopModelDriver?
    let advisor: String?

    enum CodingKeys: String, CodingKey {
        case days, points, advisor
        case last7dTokens = "last_7d_tokens"
        case previous7dTokens = "previous_7d_tokens"
        case changePct = "change_pct"
        case topModel7d = "top_model_7d"
    }
}

struct ProviderTrendPoint: Codable, Identifiable {
    var id: String { date }
    let date: String
    let totalTokens: Int

    enum CodingKeys: String, CodingKey {
        case date
        case totalTokens = "total_tokens"
    }
}

struct TopModelDriver: Codable {
    let model: String
    let label: String?
    let tokens: Int
    let sharePct: Double

    enum CodingKeys: String, CodingKey {
        case model, label, tokens
        case sharePct = "share_pct"
    }
}

enum UsagePeriod: String, CaseIterable, Identifiable {
    case day
    case week

    var id: String { rawValue }
    var label: String {
        switch self {
        case .day: return "Today"
        case .week: return "Quota week"
        }
    }
}

func distinctConversationCount(sessions: Int?, conversations: Int?) -> Int? {
    guard let conversations, conversations > 0, conversations != sessions else { return nil }
    return conversations
}

func pairedMetricLabel(
    firstLabel: String,
    firstValue: Int?,
    secondLabel: String,
    secondValue: Int?
) -> String? {
    let hasFirst = (firstValue ?? 0) > 0
    let hasSecond = (secondValue ?? 0) > 0
    if hasFirst && hasSecond { return "\(firstLabel) / \(secondLabel)" }
    if hasFirst { return firstLabel }
    if hasSecond { return secondLabel }
    return nil
}

func shouldShowQuotaCostBucket(
    _ bucket: QuotaBucket,
    selectedWindowKinds: Set<String>,
    period: UsagePeriod
) -> Bool {
    guard let windowKind = bucket.windowKind,
          selectedWindowKinds.contains(windowKind) else { return false }
    let isAggregate = bucket.scopeKind == nil || bucket.scopeKind == "aggregate"
    return !(period == .week && windowKind == "weekly" && isAggregate)
}

struct ProviderHealth: Codable {
    let quota: FeedHealth?
    let activity: FeedHealth?
    let credits: FeedHealth?
}

struct FeedHealth: Codable {
    let status: String
    let source: String
    let timestamp: Int?
    let ageSeconds: Int?
    let detail: String?
    let recovery: String?

    enum CodingKeys: String, CodingKey {
        case status, source, timestamp, detail, recovery
        case ageSeconds = "age_seconds"
    }
}

struct ProviderPeriods: Codable {
    let day: ProviderPeriodMetrics?
    let week: ProviderPeriodMetrics?

    func metrics(for period: UsagePeriod) -> ProviderPeriodMetrics? {
        period == .day ? day : week
    }
}

struct ProviderPeriodMetrics: Codable {
    let activeHours: Double?
    let messages: Int?
    let userMessages: Int?
    let sessions: Int?
    let conversations: Int?
    let inputTokens: Int?
    let outputTokens: Int?
    let cacheTokens: Int?
    let reasoningTokens: Int?
    let totalTokens: Int?
    let requests: Int?
    let models: [String: ModelTokenUsage]?
    let surfaces: [String: SurfaceUsage]?
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?
    let pricingCoveragePct: Double?
    let unpricedModels: [String]?
    let windowStartAt: String?
    let windowEndAt: String?

    enum CodingKeys: String, CodingKey {
        case messages, sessions, conversations, requests, models, surfaces
        case activeHours = "active_hours"
        case userMessages = "user_messages"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheTokens = "cache_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case pricingCoveragePct = "pricing_coverage_pct"
        case unpricedModels = "unpriced_models"
        case windowStartAt = "window_start_at"
        case windowEndAt = "window_end_at"
    }
}

struct UsageExplanation: Codable {
    let provider: String
    let period: String
    let windowStartAt: String
    let windowEndAt: String
    let estimateBasis: String
    let quotaRelation: String
    let coverage: UsageExplanationCoverage
    let totals: UsageExplanationTotals
    let categories: [UsageExplanationCategory]

    enum CodingKeys: String, CodingKey {
        case provider, period, coverage, totals, categories
        case windowStartAt = "window_start_at"
        case windowEndAt = "window_end_at"
        case estimateBasis = "estimate_basis"
        case quotaRelation = "quota_relation"
    }
}

struct UsageExplanationCoverage: Codable {
    let status: String
    let confidence: String
    let compactions: Int
    let classifiedPct: Double?

    enum CodingKeys: String, CodingKey {
        case status, confidence, compactions
        case classifiedPct = "classified_pct"
    }
}

struct UsageExplanationTotals: Codable {
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWrite5mTokens: Int
    let cacheWrite1hTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let effectiveTokens: Double
    let eventCount: Int
    let sessions: Int
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?
    let pricingCoveragePct: Double?
    let unpricedModels: [String]

    enum CodingKeys: String, CodingKey {
        case sessions
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWrite5mTokens = "cache_write_5m_tokens"
        case cacheWrite1hTokens = "cache_write_1h_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case effectiveTokens = "effective_tokens"
        case eventCount = "event_count"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case pricingCoveragePct = "pricing_coverage_pct"
        case unpricedModels = "unpriced_models"
    }
}

struct UsageExplanationCategory: Codable, Identifiable {
    let id: String
    let label: String
    let color: String
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWrite5mTokens: Int
    let cacheWrite1hTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let effectiveTokens: Double
    let sharePct: Double
    let eventCount: Int
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?
    let pricingCoveragePct: Double?
    let models: [String]
    let unpricedModels: [String]
    let confidence: String

    enum CodingKeys: String, CodingKey {
        case id, label, color, models, confidence
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWrite5mTokens = "cache_write_5m_tokens"
        case cacheWrite1hTokens = "cache_write_1h_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case effectiveTokens = "effective_tokens"
        case sharePct = "share_pct"
        case eventCount = "event_count"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case pricingCoveragePct = "pricing_coverage_pct"
        case unpricedModels = "unpriced_models"
    }
}

struct SurfaceUsage: Codable {
    let sessions: Int?
    let messages: Int?
    let userMessages: Int?
    let totalTokens: Int?
    let requests: Int?
    let models: [String: ModelTokenUsage]?
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?
    let pricingCoveragePct: Double?
    let unpricedModels: [String]?

    enum CodingKeys: String, CodingKey {
        case sessions, messages, requests, models
        case userMessages = "user_messages"
        case totalTokens = "total_tokens"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case pricingCoveragePct = "pricing_coverage_pct"
        case unpricedModels = "unpriced_models"
    }
}

struct GapRollupsPayload: Codable {
    let today: GapRollup?
    let yesterday: GapRollup?
    let last7d: GapRollup?

    enum CodingKeys: String, CodingKey {
        case today, yesterday
        case last7d = "last_7d"
    }
}

struct GapRollup: Codable {
    let focusGapSec: Int?
    let attentionIdleSec: Int?
    let offHoursAwaySec: Int?
    let agentRuntimeSec: Int?
    // Legacy: human_time_sec = focus + attention; downtime_sec retired and zeroed.
    let humanTimeSec: Int?
    let downtimeSec: Int?

    enum CodingKeys: String, CodingKey {
        case focusGapSec = "focus_gap_sec"
        case attentionIdleSec = "attention_idle_sec"
        case offHoursAwaySec = "off_hours_away_sec"
        case agentRuntimeSec = "agent_runtime_sec"
        case humanTimeSec = "human_time_sec"
        case downtimeSec = "downtime_sec"
    }
}

struct ProviderRegistryEntry: Codable {
    let id: String
    let label: String?
    let color: String?
    let order: Int?
}

struct ProviderLatestSnapshot: Codable {
    let provider: String?
    let timestamp: Int?
    let status: String?
    let plan: String?
    let shared: ProviderSharedMetrics?
    let unique: [String: JSONValue]?
    let source: [String: JSONValue]?
    let errorText: String?

    enum CodingKeys: String, CodingKey {
        case provider, timestamp, status, plan, shared, unique, source
        case errorText = "error_text"
    }
}

struct ProviderSharedMetrics: Codable {
    let primaryUsedPct: Double?
    let primaryRemainingPct: Double?
    let primaryReset: String?
    let secondaryUsedPct: Double?
    let secondaryRemainingPct: Double?
    let secondaryReset: String?
    let tokensTotalDay: Double?
    let messagesTotalDay: Double?
    let activeHoursDay: Double?

    enum CodingKeys: String, CodingKey {
        case primaryUsedPct = "primary_used_pct"
        case primaryRemainingPct = "primary_remaining_pct"
        case primaryReset = "primary_reset"
        case secondaryUsedPct = "secondary_used_pct"
        case secondaryRemainingPct = "secondary_remaining_pct"
        case secondaryReset = "secondary_reset"
        case tokensTotalDay = "tokens_total_day"
        case messagesTotalDay = "messages_total_day"
        case activeHoursDay = "active_hours_day"
    }
}

enum JSONValue: Codable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var stringValue: String? {
        switch self {
        case .string(let value):
            return value
        case .number(let value):
            if value.rounded() == value {
                return String(format: "%.0f", value)
            }
            return String(format: "%.2f", value)
        case .bool(let value):
            return value ? "true" : "false"
        default:
            return nil
        }
    }
}

/// Per-model usage for today (tokens = input + output + cache).
struct ModelTokenUsage: Codable {
    let tokens: Int?
    let requests: Int?
}

struct ClaudeToday: Codable {
    let activeHoursToday: Double?
    let messagesToday: Int?
    let inputTokensToday: Int?
    let outputTokensToday: Int?
    let threadsToday: Int?
    let sessionsToday: Int?
    let conversationsToday: Int?
    let modelsToday: [String: ModelTokenUsage]?

    enum CodingKeys: String, CodingKey {
        case activeHoursToday = "active_hours_today"
        case messagesToday = "messages_today"
        case inputTokensToday = "input_tokens_today"
        case outputTokensToday = "output_tokens_today"
        case threadsToday = "threads_today"
        case sessionsToday = "sessions_today"
        case conversationsToday = "conversations_today"
        case modelsToday = "models_today"
    }
}

struct CodexToday: Codable {
    let activeHoursToday: Double?
    let messagesToday: Int?
    let inputTokensToday: Int?
    let outputTokensToday: Int?
    let threadsToday: Int?
    let sessionsToday: Int?
    let userMessagesToday: Int?
    let reasoningTokensToday: Int?
    let modelsToday: [String: ModelTokenUsage]?

    enum CodingKeys: String, CodingKey {
        case activeHoursToday = "active_hours_today"
        case messagesToday = "messages_today"
        case inputTokensToday = "input_tokens_today"
        case outputTokensToday = "output_tokens_today"
        case threadsToday = "threads_today"
        case sessionsToday = "sessions_today"
        case userMessagesToday = "user_messages_today"
        case reasoningTokensToday = "reasoning_tokens_today"
        case modelsToday = "models_today"
    }
}

struct ClaudeTotals: Codable {
    let totalSessions: Int?
    let totalMessages: Int?
    let favoriteModel: String?

    enum CodingKeys: String, CodingKey {
        case totalSessions = "total_sessions"
        case totalMessages = "total_messages"
        case favoriteModel = "favorite_model"
    }
}

struct CodexTotals: Codable {
    let totalThreads: Int?
    let totalSessions: Int?
    let totalTokens: Int?

    enum CodingKeys: String, CodingKey {
        case totalThreads = "total_threads"
        case totalSessions = "total_sessions"
        case totalTokens = "total_tokens"
    }
}

struct QuotaBucket: Codable, Identifiable {
    let id: String
    let label: String?
    let windowKind: String?
    let windowMinutes: Double?
    let scopeKind: String?
    let model: String?
    let feature: String?
    let usedPct: Double?
    let remainingPct: Double?
    let reset: String?

    enum CodingKeys: String, CodingKey {
        case id, label, model, feature, reset
        case windowKind = "window_kind"
        case windowMinutes = "window_minutes"
        case scopeKind = "scope_kind"
        case usedPct = "used_pct"
        case remainingPct = "remaining_pct"
    }
}

struct QuotaCostEstimate: Codable, Identifiable {
    let id: String
    let estimateBasis: String?
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?
    let observedTokens: Int?
    let observedRequests: Int?
    let pricingCoveragePct: Double?
    let unpricedModels: [String]?

    enum CodingKeys: String, CodingKey {
        case id
        case estimateBasis = "estimate_basis"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case observedTokens = "observed_tokens"
        case observedRequests = "observed_requests"
        case pricingCoveragePct = "pricing_coverage_pct"
        case unpricedModels = "unpriced_models"
    }
}

struct CreditPool: Codable, Identifiable {
    let id: String
    let label: String?
    let kind: String?
    let unit: String?
    let enabled: Bool?
    let unlimited: Bool?
    let spendControlReached: Bool?
    let outOfCredits: Bool?
    let balance: Double?
    let spentMonth: Double?
    let monthlySpendCap: Double?
    let monthlySpendHeadroom: Double?
    let usedPct: Double?
    let reset: String?
    let nextExpiryAt: Int?

    enum CodingKeys: String, CodingKey {
        case id, label, kind, unit, enabled, unlimited, balance, reset
        case spendControlReached = "spend_control_reached"
        case outOfCredits = "out_of_credits"
        case spentMonth = "spent_month"
        case monthlySpendCap = "monthly_spend_cap"
        case monthlySpendHeadroom = "monthly_spend_headroom"
        case usedPct = "used_pct"
        case nextExpiryAt = "next_expiry_at"
    }
}

enum ClaudePaidUsageState: Equatable {
    case active
    case balanceEmpty
    case spendControlReached
}

struct ClaudePaidUsagePresentation: Equatable {
    let state: ClaudePaidUsageState
    let spentMonth: Double?
    let monthlySpendCap: Double?
    let monthlySpendHeadroom: Double?
    let reset: String?
}

func claudePaidUsagePresentation(
    sessionUsedPct: Double?,
    limits: [QuotaBucket]?,
    creditPools: [CreditPool]?
) -> ClaudePaidUsagePresentation? {
    let exhaustedIncludedLimit = (sessionUsedPct ?? 0) >= 99.95
        || (limits ?? []).contains { ($0.usedPct ?? 0) >= 99.95 }
    guard exhaustedIncludedLimit,
          let pool = creditPools?.first(where: {
              $0.kind != "promotional"
                  && $0.kind != "reset"
                  && $0.unit?.lowercased() == "usd"
          }) else {
        return nil
    }

    if pool.spendControlReached == true {
        return ClaudePaidUsagePresentation(
            state: .spendControlReached,
            spentMonth: pool.spentMonth,
            monthlySpendCap: pool.monthlySpendCap,
            monthlySpendHeadroom: pool.monthlySpendHeadroom,
            reset: pool.reset
        )
    }
    if pool.outOfCredits == true || (pool.balance.map { $0 <= 0 } == true) {
        return ClaudePaidUsagePresentation(
            state: .balanceEmpty,
            spentMonth: pool.spentMonth,
            monthlySpendCap: pool.monthlySpendCap,
            monthlySpendHeadroom: pool.monthlySpendHeadroom,
            reset: pool.reset
        )
    }
    let hasFunds = pool.balance.map { $0 > 0 } ?? (pool.outOfCredits == false)
    guard pool.enabled == true, hasFunds else { return nil }
    return ClaudePaidUsagePresentation(
        state: .active,
        spentMonth: pool.spentMonth,
        monthlySpendCap: pool.monthlySpendCap,
        monthlySpendHeadroom: pool.monthlySpendHeadroom,
        reset: pool.reset
    )
}

struct ClaudeQuota: Codable {
    let sessionUsedPct: Double?
    let weeklyUsedPct: Double?
    let sessionRemainingPct: Double?
    let weeklyRemainingPct: Double?
    let sessionReset: String?
    let weeklyReset: String?
    let planID: String?
    let planLabel: String?
    let planSource: String?
    let rawPlan: String?
    let planEntitlements: [String: JSONValue]?
    let limits: [QuotaBucket]?
    let creditPools: [CreditPool]?
    let costEstimates: [QuotaCostEstimate]?
    let costEstimateStatus: String?
    let costEstimateUpdatedAt: Int?

    enum CodingKeys: String, CodingKey {
        case sessionUsedPct = "session_used_pct"
        case weeklyUsedPct = "weekly_used_pct"
        case sessionRemainingPct = "session_remaining_pct"
        case weeklyRemainingPct = "weekly_remaining_pct"
        case sessionReset = "session_reset"
        case weeklyReset = "weekly_reset"
        case planID = "plan_id"
        case planLabel = "plan_label"
        case planSource = "plan_source"
        case rawPlan = "raw_plan"
        case planEntitlements = "plan_entitlements"
        case limits
        case creditPools = "credit_pools"
        case costEstimates = "cost_estimates"
        case costEstimateStatus = "cost_estimate_status"
        case costEstimateUpdatedAt = "cost_estimate_updated_at"
    }
}

struct CodexQuota: Codable {
    let timestamp: Int?
    let sessionUsedPct: Double?
    let weeklyUsedPct: Double?
    let codeReviewUsedPct: Double?
    let sessionRemainingPct: Double?
    let weeklyRemainingPct: Double?
    let codeReviewRemainingPct: Double?
    let sessionReset: String?
    let weeklyReset: String?
    let accountUsedPct: Double?
    let accountRemainingPct: Double?
    let accountReset: String?
    let accountWindowMinutes: Double?
    let planID: String?
    let planLabel: String?
    let planSource: String?
    let rawPlan: String?
    let planEntitlements: [String: JSONValue]?
    let rateLimitResetCredits: Int?
    let rateLimitResetCreditsUsedDay: Int?
    let rateLimitResetCreditsUsedWeek: Int?
    let rateLimitResetCreditsGrantedDay: Int?
    let rateLimitResetCreditsGrantedWeek: Int?
    let creditsRemaining: Double?
    let limits: [QuotaBucket]?
    let creditPools: [CreditPool]?
    let costEstimates: [QuotaCostEstimate]?
    let costEstimateStatus: String?
    let costEstimateUpdatedAt: Int?

    enum CodingKeys: String, CodingKey {
        case timestamp
        case sessionUsedPct = "session_used_pct"
        case weeklyUsedPct = "weekly_used_pct"
        case codeReviewUsedPct = "code_review_used_pct"
        case sessionRemainingPct = "session_remaining_pct"
        case weeklyRemainingPct = "weekly_remaining_pct"
        case codeReviewRemainingPct = "code_review_remaining_pct"
        case sessionReset = "session_reset"
        case weeklyReset = "weekly_reset"
        case accountUsedPct = "account_used_pct"
        case accountRemainingPct = "account_remaining_pct"
        case accountReset = "account_reset"
        case accountWindowMinutes = "account_window_minutes"
        case planID = "plan_id"
        case planLabel = "plan_label"
        case planSource = "plan_source"
        case rawPlan = "raw_plan"
        case planEntitlements = "plan_entitlements"
        case rateLimitResetCredits = "rate_limit_reset_credits"
        case rateLimitResetCreditsUsedDay = "rate_limit_reset_credits_used_day"
        case rateLimitResetCreditsUsedWeek = "rate_limit_reset_credits_used_week"
        case rateLimitResetCreditsGrantedDay = "rate_limit_reset_credits_granted_day"
        case rateLimitResetCreditsGrantedWeek = "rate_limit_reset_credits_granted_week"
        case creditsRemaining = "credits_remaining"
        case limits
        case creditPools = "credit_pools"
        case costEstimates = "cost_estimates"
        case costEstimateStatus = "cost_estimate_status"
        case costEstimateUpdatedAt = "cost_estimate_updated_at"
    }
}

struct CodexAnalyticsSummary: Codable {
    let dominantSurface: String?
    let dominantSurfaceSharePct: Double?
    let avgDailyTurns: Double?
    let avgDailyCredits: Double?
    let creditsUsed: Double?
    let creditsUsedUSD: Double?
    let creditsUsedUSDIsEstimate: Bool?
    let creditsWindowDays: Int?
    let avgDailyReviews: Double?
    let avgDailyComments: Double?
    let reviewsAvailable: Bool?

    enum CodingKeys: String, CodingKey {
        case dominantSurface = "dominant_surface"
        case dominantSurfaceSharePct = "dominant_surface_share_pct"
        case avgDailyTurns = "avg_daily_turns"
        case avgDailyCredits = "avg_daily_credits"
        case creditsUsed = "credits_used"
        case creditsUsedUSD = "credits_used_usd"
        case creditsUsedUSDIsEstimate = "credits_used_usd_is_estimate"
        case creditsWindowDays = "credits_window_days"
        case avgDailyReviews = "avg_daily_reviews"
        case avgDailyComments = "avg_daily_comments"
        case reviewsAvailable = "reviews_available"
    }
}

enum CodexPaidCreditState: Equatable {
    case active
    case balanceEmpty
    case spendControlReached
}

struct CodexPaidCreditPresentation: Equatable {
    let state: CodexPaidCreditState
    let creditsUsed: Double?
    let usdValue: Double?
    let usdIsEstimate: Bool
    let windowDays: Int?
    let balance: Double?
}

func codexPaidCreditPresentation(
    limits: [QuotaBucket]?,
    creditPools: [CreditPool]?,
    creditsUsed: Double?,
    usdValue: Double?,
    usdIsEstimate: Bool,
    windowDays: Int?
) -> CodexPaidCreditPresentation? {
    let exhaustedIncludedLimit = (limits ?? []).contains { ($0.usedPct ?? 0) >= 99.95 }
    guard exhaustedIncludedLimit,
          let pool = creditPools?.first(where: {
              $0.kind == "purchased" && $0.unit?.lowercased() == "credits"
          }) else {
        return nil
    }

    let state: CodexPaidCreditState
    if pool.spendControlReached == true {
        state = .spendControlReached
    } else if pool.enabled == false && (pool.balance ?? 0) <= 0 {
        state = .balanceEmpty
    } else if pool.enabled == true || pool.unlimited == true || (pool.balance ?? 0) > 0 {
        state = .active
    } else {
        return nil
    }
    return CodexPaidCreditPresentation(
        state: state,
        creditsUsed: creditsUsed,
        usdValue: usdValue,
        usdIsEstimate: usdIsEstimate,
        windowDays: windowDays,
        balance: pool.balance
    )
}

struct CursorModelStats: Codable {
    let requests: Int?
    let tokens: Int?
    let maxRequests: Int?

    enum CodingKeys: String, CodingKey {
        case requests, tokens
        case maxRequests = "max_requests"
    }
}

struct CursorStats: Codable {
    let plan: String?
    let totalRequests: Int?
    let totalTokens: Int?
    let startOfMonth: String?
    let maxRequests: Int?
    let remainingRequests: Int?
    let atLimit: Bool?
    let limitHit: Bool?
    let limitKind: String?
    let limitMessage: String?
    let resetAt: String?
    let spendLimitHit: Bool?
    let spendLimits: [Int]?
    let models: [String: CursorModelStats]?

    enum CodingKeys: String, CodingKey {
        case plan, models
        case totalRequests = "total_requests"
        case totalTokens = "total_tokens"
        case startOfMonth = "start_of_month"
        case maxRequests = "max_requests"
        case remainingRequests = "remaining_requests"
        case atLimit = "at_limit"
        case limitHit = "limit_hit"
        case limitKind = "limit_kind"
        case limitMessage = "limit_message"
        case resetAt = "reset_at"
        case spendLimitHit = "spend_limit_hit"
        case spendLimits = "spend_limits"
    }

    var totalMaxRequests: Int {
        if let maxRequests, maxRequests > 0 {
            return maxRequests
        }
        return (models ?? [:]).values.reduce(0) { $0 + ($1.maxRequests ?? 0) }
    }

    var usagePct: Double {
        if atLimit == true || limitHit == true {
            return 1
        }
        let max = totalMaxRequests
        guard max > 0 else { return 0 }
        return Double(totalRequests ?? 0) / Double(max)
    }

    var remaining: Int {
        if let remainingRequests {
            return remainingRequests
        }
        return totalMaxRequests - (totalRequests ?? 0)
    }

    var hasDisplayData: Bool {
        let normalizedPlan = plan?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return (normalizedPlan != nil && normalizedPlan != "unknown")
            || totalRequests != nil
            || totalTokens != nil
            || startOfMonth != nil
            || maxRequests != nil
            || remainingRequests != nil
            || atLimit != nil
            || limitHit != nil
            || limitKind != nil
            || limitMessage != nil
            || resetAt != nil
            || spendLimitHit != nil
            || !(spendLimits ?? []).isEmpty
            || !(models ?? [:]).isEmpty
    }
}

struct WeeklyPace: Codable {
    let currentWeeklyPct: Double
    let daysElapsed: Double
    let daysRemaining: Double?
    let projectedPct: Double
    let paceStatus: String?
    let onTrack: Bool

    enum CodingKeys: String, CodingKey {
        case currentWeeklyPct = "current_weekly_pct"
        case daysElapsed = "days_elapsed"
        case daysRemaining = "days_remaining"
        case projectedPct = "projected_pct"
        case paceStatus = "pace_status"
        case onTrack = "on_track"
    }
}

// MARK: - Work Tracking

struct WorkLedgerStatus: Codable {
    let schemaVersion: Int
    let settings: WorkLedgerSettings
    let activeWork: ActiveWorkContext
    let refresh: WorkRefreshState

    enum CodingKeys: String, CodingKey {
        case settings, refresh
        case schemaVersion = "schema_version"
        case activeWork = "active_work"
    }
}

struct WorkLedgerSettings: Codable {
    let collectionEnabled: Bool
    let pausedAt: Int?
    let explicitRoots: [String]
    let retentionDays: Int
    let updatedAt: Int

    enum CodingKeys: String, CodingKey {
        case collectionEnabled = "collection_enabled"
        case pausedAt = "paused_at"
        case explicitRoots = "explicit_roots"
        case retentionDays = "retention_days"
        case updatedAt = "updated_at"
    }
}

struct WorkRefreshState: Codable {
    let status: String
    let startedAt: Int?
    let finishedAt: Int?
    let lastError: String?
    let updatedAt: Int

    enum CodingKeys: String, CodingKey {
        case status
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case lastError = "last_error"
        case updatedAt = "updated_at"
    }
}

struct ActiveWorkContext: Codable {
    let state: String
    let intervalID: String?
    let startedAtUS: Int?
    let workItemID: String?
    let kind: String?
    let name: String?
    let parentID: String?
    let parentName: String?
    let repositoryID: String?
    let repositoryName: String?
    let colorHex: String?

    enum CodingKeys: String, CodingKey {
        case state, kind, name
        case intervalID = "interval_id"
        case startedAtUS = "started_at_us"
        case workItemID = "work_item_id"
        case parentID = "parent_id"
        case parentName = "parent_name"
        case repositoryID = "repository_id"
        case repositoryName = "repository_name"
        case colorHex = "color_hex"
    }
}

struct WorkItem: Codable, Identifiable {
    let id: String
    let kind: String
    let parentID: String?
    let repositoryID: String?
    let name: String
    let colorHex: String?
    let parentName: String?
    let repositoryName: String?

    enum CodingKeys: String, CodingKey {
        case id, kind, name
        case parentID = "parent_id"
        case repositoryID = "repository_id"
        case colorHex = "color_hex"
        case parentName = "parent_name"
        case repositoryName = "repository_name"
    }

    var displayName: String {
        guard let parentName, !parentName.isEmpty else { return name }
        return "\(parentName) / \(name)"
    }
}

struct WorkItemsResponse: Codable {
    let items: [WorkItem]
    let active: ActiveWorkContext
}

struct WorkItemResponse: Codable {
    let item: WorkItem
}

struct WorkRepository: Codable, Identifiable {
    let id: String
    let displayName: String
    let commonDir: String
    let enabled: Bool

    enum CodingKeys: String, CodingKey {
        case id, enabled
        case displayName = "display_name"
        case commonDir = "common_dir"
    }
}

struct WorkRepositoriesResponse: Codable {
    let repositories: [WorkRepository]
}

struct WorkSession: Codable, Identifiable {
    let id: String
    let provider: String
    let providerSessionID: String
    let displayName: String
    let nativeTitle: String?
    let nickname: String?
    let nicknameSource: String?
    let runtimeState: String
    let lastActivityAtUS: Int?
    let cwd: String?
    let repositoryID: String?
    let repositoryName: String?
    let worktreePath: String?
    let branch: String?
    let models: [String]
    let assignmentMode: String
    let assignedWorkItemID: String?
    let assignedColorHex: String?
    let effectiveWorkLabel: String
    let attributionSource: String
    var startedAtUS: Int? = nil
    var endedAtUS: Int? = nil
    var messages: Int? = nil
    var userMessages: Int? = nil
    var requests: Int? = nil
    var inputTokens: Int? = nil
    var outputTokens: Int? = nil
    var cacheTokens: Int? = nil
    var reasoningTokens: Int? = nil
    var estimatedCostUSD: Double? = nil
    var estimatedCredits: Double? = nil
    var firstPrompt: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, provider, nickname, cwd, branch, models
        case providerSessionID = "provider_session_id"
        case displayName = "display_name"
        case nativeTitle = "native_title"
        case nicknameSource = "nickname_source"
        case runtimeState = "runtime_state"
        case lastActivityAtUS = "last_activity_at_us"
        case repositoryID = "repository_id"
        case repositoryName = "repository_name"
        case worktreePath = "worktree_path"
        case assignmentMode = "assignment_mode"
        case assignedWorkItemID = "assigned_work_item_id"
        case assignedColorHex = "assigned_color_hex"
        case effectiveWorkLabel = "effective_work_label"
        case attributionSource = "attribution_source"
        case startedAtUS = "started_at_us"
        case endedAtUS = "ended_at_us"
        case messages, requests
        case userMessages = "user_messages"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheTokens = "cache_tokens"
        case reasoningTokens = "reasoning_tokens"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case firstPrompt = "first_prompt"
    }

    var totalTokens: Int {
        (inputTokens ?? 0) + (outputTokens ?? 0) + (cacheTokens ?? 0) + (reasoningTokens ?? 0)
    }

    var attributionState: WorkAttributionState {
        if assignmentMode == "unassigned" { return .excluded }
        if assignmentMode == "work_item" || attributionSource == "session" { return .confirmed }
        if attributionSource == "default" { return .confirmed }
        if repositoryID == nil { return .needsReview }
        return .automatic
    }

    var attributionReason: String {
        switch attributionState {
        case .needsReview:
            return "No repository detected"
        case .automatic:
            return branch?.isEmpty == false ? "Matched from repository and branch" : "Matched from repository"
        case .confirmed:
            return attributionSource == "default" ? "Pinned project" : "Assigned manually"
        case .excluded:
            return "Excluded from project reports"
        }
    }

    var reviewTimestampUS: Int? {
        lastActivityAtUS ?? endedAtUS ?? startedAtUS
    }

    var reviewDurationUS: Int? {
        guard let startedAtUS else { return nil }
        let end = endedAtUS ?? lastActivityAtUS
        guard let end, end >= startedAtUS else { return nil }
        return end - startedAtUS
    }

    var reviewChildIdentifier: String {
        let compactID = providerSessionID.replacingOccurrences(of: "-", with: "")
        let suffix = compactID.suffix(8)
        return "\(provider.capitalized) · \(suffix)"
    }

    fileprivate var reviewGroupKey: String {
        let title = displayName
            .split(whereSeparator: \.isWhitespace)
            .joined(separator: " ")
            .lowercased()
        let repository = repositoryID ?? repositoryName ?? cwd ?? "unassigned"
        let normalizedBranch = (branch ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return "\(repository)|\(normalizedBranch)|\(title)"
    }
}

struct WorkSessionReviewGroup: Identifiable {
    let id: String
    let sessions: [WorkSession]

    var representative: WorkSession { sessions[0] }
    var displayName: String { representative.displayName }
    var isCollection: Bool { sessions.count > 1 }
    var totalTokens: Int { sessions.reduce(0) { $0 + $1.totalTokens } }
    var latestTimestampUS: Int? { sessions.compactMap(\.reviewTimestampUS).max() }

    var providerSummary: String {
        Array(Set(sessions.map { $0.provider.capitalized }))
            .sorted()
            .joined(separator: " + ")
    }

    var runtimeState: String {
        for state in ["running", "active", "idle"] where sessions.contains(where: {
            $0.runtimeState == state
        }) {
            return state
        }
        return "ended"
    }

    var attributionLabel: String {
        let states = Set(sessions.map { $0.attributionState.rawValue })
        guard states.count == 1, let state = sessions.first?.attributionState else {
            return "Mixed attribution"
        }
        switch state {
        case .needsReview: return "Needs review"
        case .automatic: return "Automatic"
        case .confirmed: return "Confirmed"
        case .excluded: return "Excluded"
        }
    }

    var attributionSummary: String {
        let values: [(WorkAttributionState, String)] = [
            (.needsReview, "review"),
            (.automatic, "automatic"),
            (.confirmed, "confirmed"),
            (.excluded, "excluded"),
        ]
        return values.compactMap { value in
            let (state, label) = value
            let count = sessions.filter { $0.attributionState == state }.count
            return count > 0 ? "\(count) \(label)" : nil
        }.joined(separator: " · ")
    }
}

extension Array where Element == WorkSession {
    func groupedForReview() -> [WorkSessionReviewGroup] {
        var orderedKeys: [String] = []
        var sessionsByKey: [String: [WorkSession]] = [:]
        for session in self {
            let key = session.reviewGroupKey
            if sessionsByKey[key] == nil {
                orderedKeys.append(key)
            }
            sessionsByKey[key, default: []].append(session)
        }
        return orderedKeys.compactMap { key in
            guard let sessions = sessionsByKey[key], !sessions.isEmpty else { return nil }
            return WorkSessionReviewGroup(
                id: "review-group|\(key)",
                sessions: sessions.sorted {
                    ($0.reviewTimestampUS ?? 0) > ($1.reviewTimestampUS ?? 0)
                }
            )
        }
    }
}

enum WorkAttributionState: String {
    case needsReview
    case automatic
    case confirmed
    case excluded
}

struct WorkSessionsResponse: Codable {
    let sessions: [WorkSession]
}

struct SessionEvidenceResponse: Codable {
    let sessionID: String
    let provider: String
    let providerSessionID: String
    let items: [SessionEvidenceItem]
    let truncated: Bool
    let sourceMissing: Bool
    let bytesScanned: Int

    enum CodingKeys: String, CodingKey {
        case provider, items, truncated
        case sessionID = "session_id"
        case providerSessionID = "provider_session_id"
        case sourceMissing = "source_missing"
        case bytesScanned = "bytes_scanned"
    }
}

struct SessionEvidenceItem: Codable, Identifiable {
    let id: String
    let occurredAtUS: Int
    let kind: String
    let title: String
    let summary: String
    let toolName: String?
    let commandCategory: String?
    let filePaths: [String]

    enum CodingKeys: String, CodingKey {
        case id, kind, title, summary
        case occurredAtUS = "occurred_at_us"
        case toolName = "tool_name"
        case commandCategory = "command_category"
        case filePaths = "file_paths"
    }
}

struct SessionEvidenceSearchResponse: Codable {
    let query: String
    let matches: [SessionEvidenceMatch]
    let sessionsScanned: Int
    let sessionsAvailable: Int
    let bytesScanned: Int
    let missingSources: Int
    let partial: Bool
    let index: SessionSearchIndexStatus?

    enum CodingKeys: String, CodingKey {
        case query, matches, partial, index
        case sessionsScanned = "sessions_scanned"
        case sessionsAvailable = "sessions_available"
        case bytesScanned = "bytes_scanned"
        case missingSources = "missing_sources"
    }
}

struct SessionSearchIndexStatus: Codable {
    let state: String
    let backend: String
    let privacyMode: String
    let sessionsIndexed: Int
    let sessionsAvailable: Int
    let documents: Int
    let staleSessions: Int
    let missingSources: Int
    let lastIndexedAtUS: Int?
    let dbBytes: Int
    let lastError: String?

    enum CodingKeys: String, CodingKey {
        case state, backend, documents
        case privacyMode = "privacy_mode"
        case sessionsIndexed = "sessions_indexed"
        case sessionsAvailable = "sessions_available"
        case staleSessions = "stale_sessions"
        case missingSources = "missing_sources"
        case lastIndexedAtUS = "last_indexed_at_us"
        case dbBytes = "db_bytes"
        case lastError = "last_error"
    }
}

struct SessionEvidenceMatch: Codable, Identifiable {
    let sessionID: String
    let provider: String
    let sessionName: String
    let repositoryName: String?
    let branch: String?
    let evidenceID: String?
    let occurredAtUS: Int?
    let kind: String
    let title: String
    let snippet: String

    var id: String {
        evidenceID ?? "\(sessionID):session"
    }

    enum CodingKeys: String, CodingKey {
        case provider, branch, kind, title, snippet
        case sessionID = "session_id"
        case sessionName = "session_name"
        case repositoryName = "repository_name"
        case evidenceID = "evidence_id"
        case occurredAtUS = "occurred_at_us"
    }
}

enum WorkReportPeriod: String, CaseIterable, Identifiable {
    case day
    case week
    case month

    var id: String { rawValue }

    var label: String {
        switch self {
        case .day: return "Today"
        case .week: return "7 Days"
        case .month: return "30 Days"
        }
    }
}

struct WorkReport: Codable {
    let period: String
    let generatedAt: Int
    let window: WorkReportWindow
    let totals: WorkReportMetrics
    let unassigned: WorkReportMetrics
    let projects: [WorkReportBucket]
    let workItems: [WorkReportBucket]
    let repositories: [WorkReportBucket]
    let providers: [WorkReportBucket]

    enum CodingKeys: String, CodingKey {
        case period, window, totals, unassigned, projects, repositories, providers
        case generatedAt = "generated_at"
        case workItems = "work_items"
    }
}

struct WorkReportWindow: Codable {
    let startAt: Int
    let endAt: Int
    let timezone: String

    enum CodingKeys: String, CodingKey {
        case timezone
        case startAt = "start_at"
        case endAt = "end_at"
    }
}

struct WorkReportMetrics: Codable {
    let trackedSeconds: Double
    let activeSeconds: Double?
    let sessionCount: Int
    let messages: Int
    let userMessages: Int
    let requests: Int
    let inputTokens: Int
    let outputTokens: Int
    let cacheTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?
    let pricingCoveragePct: Double?
    let unpricedModels: [String]
    let activityEvents: Int
    let gitCommits: Int
    let gitPullRequests: Int?
    let mergeCommits: Int?
    let filesChanged: Int
    let additions: Int
    let deletions: Int
    let changedLines: Int?
    // Cost-to-outcome ratios, decoded from the server rather than derived here. nil
    // means the server could not state the ratio honestly (zero denominator,
    // unpriced cost, or no measured AI usage), and must render as "not stateable"
    // rather than as a zero. Dividing cost by a count on this side would reintroduce
    // exactly the misreading the server suppression exists to prevent.
    let costPerPR: Double?
    let costPerCommit: Double?
    let costPerChangedLine: Double?
    let models: [WorkReportModelUsage]
    let eventKinds: [String: Int]
    let usageCategories: [WorkReportUsageCategory]

    enum CodingKeys: String, CodingKey {
        case messages, requests, models, additions, deletions
        case trackedSeconds = "tracked_seconds"
        case activeSeconds = "active_seconds"
        case sessionCount = "session_count"
        case userMessages = "user_messages"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheTokens = "cache_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
        case pricingCoveragePct = "pricing_coverage_pct"
        case unpricedModels = "unpriced_models"
        case activityEvents = "activity_events"
        case gitCommits = "git_commits"
        case gitPullRequests = "git_pull_requests"
        case mergeCommits = "merge_commits"
        case filesChanged = "files_changed"
        case changedLines = "changed_lines"
        case costPerPR = "cost_per_pr"
        case costPerCommit = "cost_per_commit"
        case costPerChangedLine = "cost_per_changed_line"
        case eventKinds = "event_kinds"
        case usageCategories = "usage_categories"
    }

    var pullRequestCount: Int { gitPullRequests ?? eventKinds["git_pull_request"] ?? 0 }
    var changedLineCount: Int { changedLines ?? additions + deletions }
    var reportedSeconds: Double { activeSeconds ?? trackedSeconds }

    var hasActivity: Bool {
        reportedSeconds > 0 || totalTokens > 0 || activityEvents > 0
    }
}

struct WorkReportUsageCategory: Codable, Identifiable {
    let id: String
    let label: String
    let color: String
    let totalTokens: Int
    let effectiveTokens: Double
    let sharePct: Double
    let eventCount: Int
    let estimatedCostUSD: Double?
    let estimatedCredits: Double?

    enum CodingKeys: String, CodingKey {
        case id, label, color
        case totalTokens = "total_tokens"
        case effectiveTokens = "effective_tokens"
        case sharePct = "share_pct"
        case eventCount = "event_count"
        case estimatedCostUSD = "estimated_cost_usd"
        case estimatedCredits = "estimated_credits"
    }
}

struct WorkReportModelUsage: Codable, Identifiable {
    let model: String
    let tokens: Int
    let requests: Int

    var id: String { model }
}

struct WorkReportBucket: Codable, Identifiable {
    let id: String?
    let name: String
    let kind: String?
    let parentID: String?
    let parentName: String?
    let repositoryID: String?
    let repositoryName: String?
    let colorHex: String?
    let enabled: Bool?
    let inferred: Bool?
    let attributionSource: String?
    let attributionConfidence: Double?
    let metrics: WorkReportMetrics

    enum CodingKeys: String, CodingKey {
        case id, name, kind, enabled, inferred, metrics
        case parentID = "parent_id"
        case parentName = "parent_name"
        case repositoryID = "repository_id"
        case repositoryName = "repository_name"
        case colorHex = "color_hex"
        case attributionSource = "attribution_source"
        case attributionConfidence = "attribution_confidence"
    }

    var stableID: String {
        id ?? "unassigned-\(kind ?? "bucket")"
    }
}
