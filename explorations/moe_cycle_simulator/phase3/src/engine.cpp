#include "moe_sim/engine.hpp"

#include <algorithm>
#include <array>
#include <functional>
#include <iomanip>
#include <limits>
#include <numeric>
#include <sstream>

namespace moe_sim {
namespace {

using boost::multiprecision::cpp_int;
constexpr std::uint64_t kFsPerSecond = 1'000'000'000'000'000ULL;
constexpr std::uint64_t kMaxEvents = 1'000'000;
constexpr std::uint64_t kMaxSameTimeEvents = 100'000;
constexpr std::uint64_t kMaxWaitForNodes = 100'000;

U128 checked_u128(const cpp_int& value, const std::string& name) {
  const cpp_int maximum = (cpp_int{1} << 128) - 1;
  if (value < 0 || value > maximum) {
    throw EngineError(name + " exceeds unsigned 128-bit range");
  }
  return static_cast<U128>(value);
}

U128 checked_add_u128(
    const U128& left,
    const U128& right,
    const std::string& name) {
  return checked_u128(cpp_int{left} + cpp_int{right}, name);
}

std::uint64_t checked_u64(const cpp_int& value, const std::string& name) {
  if (value < 0 || value > std::numeric_limits<std::uint64_t>::max()) {
    throw EngineError(name + " exceeds unsigned 64-bit range");
  }
  return static_cast<std::uint64_t>(value);
}

std::uint32_t rotate_right(std::uint32_t value, unsigned amount) {
  return (value >> amount) | (value << (32U - amount));
}

std::string sha256_hex(const std::string& input) {
  static constexpr std::array<std::uint32_t, 64> constants{
      0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
      0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
      0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
      0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
      0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
      0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
      0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
      0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
      0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
      0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
      0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
      0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
      0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
      0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
      0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
      0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U};
  std::vector<std::uint8_t> bytes(input.begin(), input.end());
  const std::uint64_t bit_length =
      static_cast<std::uint64_t>(bytes.size()) * 8ULL;
  bytes.push_back(0x80U);
  while (bytes.size() % 64 != 56) {
    bytes.push_back(0);
  }
  for (int shift = 56; shift >= 0; shift -= 8) {
    bytes.push_back(
        static_cast<std::uint8_t>((bit_length >> shift) & 0xffU));
  }
  std::array<std::uint32_t, 8> hash{
      0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
      0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U};
  for (std::size_t offset = 0; offset < bytes.size(); offset += 64) {
    std::array<std::uint32_t, 64> words{};
    for (std::size_t index = 0; index < 16; ++index) {
      const std::size_t position = offset + index * 4;
      words[index] =
          (static_cast<std::uint32_t>(bytes[position]) << 24U) |
          (static_cast<std::uint32_t>(bytes[position + 1]) << 16U) |
          (static_cast<std::uint32_t>(bytes[position + 2]) << 8U) |
          static_cast<std::uint32_t>(bytes[position + 3]);
    }
    for (std::size_t index = 16; index < 64; ++index) {
      const std::uint32_t s0 =
          rotate_right(words[index - 15], 7) ^
          rotate_right(words[index - 15], 18) ^
          (words[index - 15] >> 3U);
      const std::uint32_t s1 =
          rotate_right(words[index - 2], 17) ^
          rotate_right(words[index - 2], 19) ^
          (words[index - 2] >> 10U);
      words[index] =
          words[index - 16] + s0 + words[index - 7] + s1;
    }
    auto work = hash;
    for (std::size_t index = 0; index < 64; ++index) {
      const std::uint32_t sum1 =
          rotate_right(work[4], 6) ^ rotate_right(work[4], 11) ^
          rotate_right(work[4], 25);
      const std::uint32_t choose =
          (work[4] & work[5]) ^ (~work[4] & work[6]);
      const std::uint32_t temp1 =
          work[7] + sum1 + choose + constants[index] + words[index];
      const std::uint32_t sum0 =
          rotate_right(work[0], 2) ^ rotate_right(work[0], 13) ^
          rotate_right(work[0], 22);
      const std::uint32_t majority =
          (work[0] & work[1]) ^ (work[0] & work[2]) ^
          (work[1] & work[2]);
      const std::uint32_t temp2 = sum0 + majority;
      work[7] = work[6];
      work[6] = work[5];
      work[5] = work[4];
      work[4] = work[3] + temp1;
      work[3] = work[2];
      work[2] = work[1];
      work[1] = work[0];
      work[0] = temp1 + temp2;
    }
    for (std::size_t index = 0; index < hash.size(); ++index) {
      hash[index] += work[index];
    }
  }
  std::ostringstream stream;
  stream << std::hex << std::setfill('0');
  for (const std::uint32_t word : hash) {
    stream << std::setw(8) << word;
  }
  return stream.str();
}

void append_field(std::string& output, const std::string& value) {
  output += std::to_string(value.size());
  output.push_back(':');
  output += value;
}

class FieldReader {
 public:
  explicit FieldReader(const std::string& input) : input_(input) {}

  std::string next() {
    if (position_ >= input_.size()) {
      throw EngineError("truncated checkpoint field");
    }
    const std::size_t colon = input_.find(':', position_);
    if (colon == std::string::npos || colon == position_) {
      throw EngineError("malformed checkpoint field length");
    }
    const std::string length_text =
        input_.substr(position_, colon - position_);
    if ((length_text.size() > 1 && length_text.front() == '0') ||
        !std::all_of(
            length_text.begin(), length_text.end(), [](char character) {
              return character >= '0' && character <= '9';
            })) {
      throw EngineError("non-canonical checkpoint field length");
    }
    std::size_t length = 0;
    try {
      length = std::stoull(length_text);
    } catch (const std::exception&) {
      throw EngineError("invalid checkpoint field length");
    }
    position_ = colon + 1;
    if (length > input_.size() - position_) {
      throw EngineError("truncated checkpoint field payload");
    }
    std::string result = input_.substr(position_, length);
    position_ += length;
    return result;
  }

  void require_end() const {
    if (position_ != input_.size()) {
      throw EngineError("trailing checkpoint bytes");
    }
  }

 private:
  const std::string& input_;
  std::size_t position_{0};
};

std::uint64_t parse_u64_field(
    const std::string& value, const std::string& name) {
  if (value.empty() || (value.size() > 1 && value.front() == '0') ||
      !std::all_of(value.begin(), value.end(), [](char character) {
        return character >= '0' && character <= '9';
      })) {
    throw EngineError("invalid " + name);
  }
  try {
    return std::stoull(value);
  } catch (const std::exception&) {
    throw EngineError(name + " exceeds uint64");
  }
}

std::uint32_t parse_u32_field(
    const std::string& value, const std::string& name) {
  const std::uint64_t parsed = parse_u64_field(value, name);
  if (parsed > std::numeric_limits<std::uint32_t>::max()) {
    throw EngineError(name + " exceeds uint32");
  }
  return static_cast<std::uint32_t>(parsed);
}

std::size_t parse_size_field(
    const std::string& value, const std::string& name) {
  const std::uint64_t parsed = parse_u64_field(value, name);
  if (parsed > std::numeric_limits<std::size_t>::max()) {
    throw EngineError(name + " exceeds size_t");
  }
  return static_cast<std::size_t>(parsed);
}

template <typename Enum>
Enum parse_enum_field(
    const std::string& value, int maximum, const std::string& name) {
  const std::uint64_t parsed = parse_u64_field(value, name);
  if (parsed > static_cast<std::uint64_t>(maximum)) {
    throw EngineError("invalid " + name);
  }
  return static_cast<Enum>(parsed);
}

bool same_config(const EngineConfig& left, const EngineConfig& right) {
  return left.max_events == right.max_events &&
         left.max_same_time_events == right.max_same_time_events &&
         left.max_wait_for_nodes == right.max_wait_for_nodes &&
         left.phase2_ledger_sha256 == right.phase2_ledger_sha256 &&
         left.canonical_bundle_semantic_root ==
             right.canonical_bundle_semantic_root &&
         left.engine_build_sha256 == right.engine_build_sha256 &&
         left.engine_profile_sha256 == right.engine_profile_sha256 &&
         left.checkpoint_schema_sha256 == right.checkpoint_schema_sha256;
}

bool is_sha256(const std::string& value) {
  return value.size() == 64 &&
         std::all_of(value.begin(), value.end(), [](const char character) {
           return (character >= '0' && character <= '9') ||
                  (character >= 'a' && character <= 'f');
         });
}

}  // namespace

std::string sha256_bytes(const std::string& value) {
  return sha256_hex(value);
}

std::string to_decimal(const U128& value) { return value.str(); }

U128 parse_u128(const std::string& value) {
  if (value.empty() || (value.size() > 1 && value.front() == '0')) {
    throw EngineError("non-canonical unsigned decimal string");
  }
  cpp_int parsed{0};
  for (const char character : value) {
    if (character < '0' || character > '9') {
      throw EngineError("non-canonical unsigned decimal string");
    }
    parsed *= 10;
    parsed += character - '0';
  }
  return checked_u128(parsed, "decimal value");
}

void Clock::validate() const {
  if (clock_id.empty() || frequency_numerator_hz == 0 ||
      frequency_denominator_hz == 0) {
    throw EngineError("invalid clock identity or zero frequency");
  }
  if (std::gcd(frequency_numerator_hz, frequency_denominator_hz) != 1) {
    throw EngineError("clock frequency is not gcd-normalized");
  }
  if (cpp_int{frequency_numerator_hz} >
      cpp_int{kFsPerSecond} * frequency_denominator_hz) {
    throw EngineError("clock frequency exceeds 1 PHz");
  }
  if (fractional_remainder != remainder(local_cycle)) {
    throw EngineError("clock fractional remainder mismatch");
  }
  static_cast<void>(edge_time(local_cycle));
}

U128 Clock::edge_time(std::uint64_t cycle) const {
  const cpp_int numerator =
      cpp_int{cycle} * kFsPerSecond * frequency_denominator_hz;
  return checked_u128(
      cpp_int{phase_offset_fs} + numerator / frequency_numerator_hz,
      "clock edge time");
}

std::uint64_t Clock::remainder(std::uint64_t cycle) const {
  const cpp_int value =
      (cpp_int{cycle} * kFsPerSecond * frequency_denominator_hz) %
      frequency_numerator_hz;
  return checked_u64(value, "clock fractional remainder");
}

std::uint64_t Clock::ceil_edge(const U128& time_fs) const {
  if (time_fs <= phase_offset_fs) {
    return 0;
  }
  const cpp_int delta = cpp_int{time_fs} - cpp_int{phase_offset_fs};
  const cpp_int numerator = delta * frequency_numerator_hz;
  const cpp_int denominator =
      cpp_int{kFsPerSecond} * frequency_denominator_hz;
  cpp_int estimate = (numerator + denominator - 1) / denominator;
  auto cycle = checked_u64(estimate, "clock ceil edge");
  while (cycle > 0 && edge_time(cycle - 1) >= time_fs) {
    --cycle;
  }
  while (edge_time(cycle) < time_fs) {
    if (cycle == std::numeric_limits<std::uint64_t>::max()) {
      throw EngineError("clock ceil edge overflows uint64");
    }
    ++cycle;
  }
  return cycle;
}

void Bridge::validate(const std::map<std::string, Clock>& clocks) const {
  if (!clocks.contains(source_clock_id) ||
      !clocks.contains(target_clock_id) || queue_capacity == 0) {
    throw EngineError("bridge references unknown clock or has zero capacity");
  }
  if (protocol == BridgeProtocol::kOneWay &&
      (reverse_latency_fs != 0 || ack_sync_cycles != 0)) {
    throw EngineError("ONE_WAY bridge cannot define acknowledge path");
  }
  if (protocol == BridgeProtocol::kCredit &&
      backpressure_policy != BackpressurePolicy::kCreditBlock) {
    throw EngineError("CREDIT protocol requires CREDIT_BLOCK");
  }
  if (protocol != BridgeProtocol::kCredit &&
      backpressure_policy == BackpressurePolicy::kCreditBlock) {
    throw EngineError("CREDIT_BLOCK requires CREDIT protocol");
  }
  if (forward_latency_fs == 0 && receiver_sync_cycles == 0) {
    throw EngineError("bridge request path lacks strict progress");
  }
}

U128 Bridge::arrival(
    const U128& source_completion_fs,
    const std::map<std::string, Clock>& clocks) const {
  validate(clocks);
  const Clock& target = clocks.at(target_clock_id);
  const U128 capture_time = checked_u128(
      cpp_int{source_completion_fs} + cpp_int{forward_latency_fs},
      "bridge forward arrival");
  const std::uint64_t capture_cycle = target.ceil_edge(capture_time);
  if (receiver_sync_cycles >
      std::numeric_limits<std::uint64_t>::max() - capture_cycle) {
    throw EngineError("bridge synchronization cycle overflow");
  }
  return target.edge_time(capture_cycle + receiver_sync_cycles);
}

Engine::Engine(
    std::vector<Event> events,
    std::vector<Resource> resources,
    EngineConfig config,
    std::vector<Clock> clocks)
    : Engine(
          std::move(events), std::move(resources), std::move(config),
          std::move(clocks), RestoreConstructionTag{}) {
  for (const auto& [id, resource] : resources_) {
    static_cast<void>(id);
    if (!resource.waiters.empty()) {
      throw EngineError("fresh resources cannot contain waiters");
    }
  }
}

Engine::Engine(
    std::vector<Event> events,
    std::vector<Resource> resources,
    EngineConfig config,
    std::vector<Clock> clocks,
    RestoreConstructionTag)
    : events_(std::move(events)), config_(std::move(config)) {
  std::sort(events_.begin(), events_.end(), [](const Event& left, const Event& right) {
    return left.key < right.key;
  });
  for (auto& resource : resources) {
    if (!resources_.emplace(resource.resource_id, std::move(resource)).second) {
      throw EngineError("duplicate resource ID");
    }
  }
  for (auto& clock : clocks) {
    clock.validate();
    if (!clocks_.emplace(clock.clock_id, std::move(clock)).second) {
      throw EngineError("duplicate clock ID");
    }
  }
  for (const auto& event : events_) {
    if (!states_.emplace(event.key.event_id, EventState::kPending).second) {
      throw EngineError("duplicate event ID");
    }
  }
  validate_inputs();
}

void Engine::validate_inputs() const {
  if (config_.max_events == 0 || config_.max_same_time_events == 0 ||
      config_.max_wait_for_nodes == 0) {
    throw EngineError("engine limits must be non-zero");
  }
  if (config_.max_events > kMaxEvents ||
      config_.max_same_time_events > kMaxSameTimeEvents ||
      config_.max_wait_for_nodes > kMaxWaitForNodes) {
    throw EngineError("engine limits exceed frozen profile maxima");
  }
  if (!is_sha256(config_.phase2_ledger_sha256) ||
      !is_sha256(config_.canonical_bundle_semantic_root) ||
      !is_sha256(config_.engine_build_sha256) ||
      !is_sha256(config_.engine_profile_sha256) ||
      !is_sha256(config_.checkpoint_schema_sha256)) {
    throw EngineError(
        "contract hashes must be lowercase 64-character SHA-256");
  }
  if (events_.size() > config_.max_events) {
    throw EngineError("input event count exceeds max_events");
  }
  std::set<EventKey> keys;
  for (const auto& event : events_) {
    if (event.key.event_id.empty() || event.key.component_id.empty() ||
        event.owner_id.empty() || event.quantity == 0 ||
        !keys.insert(event.key).second) {
      throw EngineError("invalid or duplicate complete event key");
    }
    if (!resources_.contains(event.resource_id)) {
      throw EngineError("event references unknown resource");
    }
    const Resource& resource = resources_.at(event.resource_id);
    if (resource.kind == ResourceKind::kUnsupportedPhase4 ||
        event.action == Action::kTransferUnsupported) {
      throw EngineError("UNSUPPORTED_PHASE4_RESOURCE");
    }
    if (resource.arbitration == Arbitration::kRoundRobinUnsupported) {
      throw EngineError("ROUND_ROBIN_UNSUPPORTED");
    }
    for (const auto& dependency : event.dependencies) {
      if (!states_.contains(dependency) ||
          dependency == event.key.event_id) {
        throw EngineError("missing or self dependency");
      }
      const auto dependency_it = std::find_if(
          events_.begin(), events_.end(), [&](const Event& candidate) {
            return candidate.key.event_id == dependency;
          });
      if (dependency_it == events_.end() ||
          !(dependency_it->key < event.key)) {
        throw EngineError("dependency does not precede consumer");
      }
    }
  }
  std::size_t total_holders = 0;
  std::size_t total_waiters = 0;
  for (const auto& [id, resource] : resources_) {
    if (id.empty() || resource.capacity == 0 ||
        resource.occupancy > resource.capacity) {
      throw EngineError("invalid resource capacity or occupancy");
    }
    if (resource.kind == ResourceKind::kUnsupportedPhase4) {
      throw EngineError("UNSUPPORTED_PHASE4_RESOURCE");
    }
    if (resource.arbitration == Arbitration::kRoundRobinUnsupported) {
      throw EngineError("ROUND_ROBIN_UNSUPPORTED");
    }
    cpp_int held{0};
    if (resource.holders.size() >
            config_.max_events - total_holders ||
        resource.waiters.size() >
            config_.max_events - total_waiters) {
      throw EngineError("resource state exceeds configured limits");
    }
    total_holders += resource.holders.size();
    total_waiters += resource.waiters.size();
    for (const auto& [owner, quantity] : resource.holders) {
      if (owner.empty() || quantity == 0) {
        throw EngineError("invalid resource holder");
      }
      held += cpp_int{quantity};
      if (held > cpp_int{resource.capacity}) {
        throw EngineError("resource holder conservation mismatch");
      }
    }
    if (held != cpp_int{resource.occupancy}) {
      throw EngineError("resource holder conservation mismatch");
    }
  }
  for (const auto& [id, clock] : clocks_) {
    if (id != clock.clock_id) {
      throw EngineError("clock map identity mismatch");
    }
    clock.validate();
  }
}

void Engine::validate_runtime_state() const {
  if (states_.size() != events_.size()) {
    throw EngineError("checkpoint event/state key-set mismatch");
  }
  std::map<std::string, const Event*> by_id;
  for (const auto& event : events_) {
    by_id.emplace(event.key.event_id, &event);
  }
  for (const auto& [id, state] : states_) {
    if (!by_id.contains(id) || state == EventState::kFailed) {
      throw EngineError("checkpoint event/state key-set mismatch");
    }
  }
  validate_inputs();

  std::set<std::string> queued_waiters;
  for (const auto& [resource_id, resource] : resources_) {
    for (const auto& waiter_id : resource.waiters) {
      if (!queued_waiters.insert(waiter_id).second ||
          !by_id.contains(waiter_id)) {
        throw EngineError("checkpoint waiter identity mismatch");
      }
      const Event& waiter = *by_id.at(waiter_id);
      if (waiter.action != Action::kAcquire ||
          waiter.resource_id != resource_id ||
          states_.at(waiter_id) != EventState::kBlocked) {
        throw EngineError("checkpoint waiter/state mismatch");
      }
    }
  }
  for (const auto& [id, state] : states_) {
    if ((state == EventState::kBlocked) != queued_waiters.contains(id)) {
      throw EngineError("checkpoint blocked waiter/state mismatch");
    }
  }

  std::map<std::string, std::uint64_t> blocked_trace_count;
  std::map<std::string, std::uint64_t> complete_trace_count;
  std::optional<U128> previous_execution_time;
  for (const auto& entry : trace_) {
    if (!by_id.contains(entry.event_id)) {
      throw EngineError("checkpoint trace references missing event");
    }
    const Event& event = *by_id.at(entry.event_id);
    if (entry.scheduled_time_fs != event.key.time_fs ||
        entry.executed_time_fs < entry.scheduled_time_fs ||
        entry.executed_time_fs > global_time_fs_ ||
        (previous_execution_time.has_value() &&
         entry.executed_time_fs < *previous_execution_time)) {
      throw EngineError("checkpoint trace time monotonicity mismatch");
    }
    previous_execution_time = entry.executed_time_fs;
    if (entry.state == EventState::kBlocked) {
      ++blocked_trace_count[entry.event_id];
    } else if (entry.state == EventState::kComplete) {
      ++complete_trace_count[entry.event_id];
    } else {
      throw EngineError("checkpoint trace contains invalid state");
    }
  }

  std::uint64_t completed_states = 0;
  for (const auto& [id, state] : states_) {
    const std::uint64_t blocked_count = blocked_trace_count[id];
    const std::uint64_t complete_count = complete_trace_count[id];
    if (blocked_count > 1 || complete_count > 1 ||
        (state == EventState::kPending &&
         (blocked_count != 0 || complete_count != 0)) ||
        (state == EventState::kBlocked &&
         (blocked_count != 1 || complete_count != 0)) ||
        (state == EventState::kComplete && complete_count != 1)) {
      throw EngineError("checkpoint trace/state consistency mismatch");
    }
    if (state == EventState::kComplete) {
      ++completed_states;
    }
  }
  if (processed_count_ != completed_states ||
      processed_count_ > config_.max_events) {
    throw EngineError("checkpoint processed-count mismatch");
  }

  if (trace_.empty()) {
    if (same_time_count_ != 0 || last_event_time_fs_.has_value() ||
        global_time_fs_ != 0) {
      throw EngineError("checkpoint empty-trace counter/time mismatch");
    }
  } else {
    const U128 tail_time = trace_.back().executed_time_fs;
    std::uint64_t tail_count = 0;
    for (auto iterator = trace_.rbegin();
         iterator != trace_.rend() &&
         iterator->executed_time_fs == tail_time;
         ++iterator) {
      ++tail_count;
    }
    if (!last_event_time_fs_.has_value() ||
        *last_event_time_fs_ != tail_time ||
        global_time_fs_ != tail_time ||
        same_time_count_ != tail_count) {
      throw EngineError("checkpoint trace-tail counter/time mismatch");
    }
  }
  if (terminal_status_ == TerminalStatus::kQuiescent &&
      (has_pending_events() || has_blocked_events())) {
    throw EngineError("checkpoint quiescent terminal mismatch");
  }
  if (terminal_status_ == TerminalStatus::kDeadlock &&
      ((!has_blocked_events() && !has_pending_events()) ||
       next_ready_pending_index().has_value())) {
    throw EngineError("checkpoint deadlock terminal mismatch");
  }
  if (terminal_status_ == TerminalStatus::kZeno &&
      same_time_count_ != config_.max_same_time_events) {
    throw EngineError("checkpoint Zeno terminal mismatch");
  }
  if (terminal_status_ != TerminalStatus::kZeno &&
      same_time_count_ > config_.max_same_time_events) {
    throw EngineError("checkpoint same-time budget mismatch");
  }
  if (terminal_status_ == TerminalStatus::kFailed) {
    throw EngineError("FAILED checkpoints are NON_CHECKPOINTABLE");
  }

  std::map<std::string, Resource> initial_resources = resources_;
  for (auto& [id, resource] : initial_resources) {
    static_cast<void>(id);
    resource.waiters.clear();
  }
  for (auto iterator = trace_.rbegin(); iterator != trace_.rend();
       ++iterator) {
    if (iterator->state != EventState::kComplete) {
      continue;
    }
    const Event& event = *by_id.at(iterator->event_id);
    Resource& resource = initial_resources.at(event.resource_id);
    if (event.action == Action::kAcquire) {
      const auto holder = resource.holders.find(event.owner_id);
      if (holder == resource.holders.end() ||
          holder->second < event.quantity ||
          resource.occupancy < event.quantity) {
        throw EngineError("checkpoint history reverse-acquire mismatch");
      }
      holder->second -= event.quantity;
      resource.occupancy -= event.quantity;
      if (holder->second == 0) {
        resource.holders.erase(holder);
      }
    } else if (event.action == Action::kRelease) {
      if (event.quantity > resource.capacity - resource.occupancy) {
        throw EngineError("checkpoint history reverse-release mismatch");
      }
      resource.occupancy = checked_add_u128(
          resource.occupancy, event.quantity,
          "checkpoint reverse-release occupancy");
      resource.holders[event.owner_id] = checked_add_u128(
          resource.holders[event.owner_id], event.quantity,
          "checkpoint reverse-release holder");
    }
  }

  std::vector<Resource> replay_resources;
  replay_resources.reserve(initial_resources.size());
  for (const auto& [id, resource] : initial_resources) {
    static_cast<void>(id);
    replay_resources.push_back(resource);
  }
  std::vector<Clock> replay_clocks;
  replay_clocks.reserve(clocks_.size());
  for (const auto& [id, clock] : clocks_) {
    static_cast<void>(id);
    replay_clocks.push_back(clock);
  }
  Engine replay(events_, std::move(replay_resources), config_,
                std::move(replay_clocks));
  const auto trace_entry_equal = [](const TraceEntry& left,
                                    const TraceEntry& right) {
    return left.event_id == right.event_id &&
           left.scheduled_time_fs == right.scheduled_time_fs &&
           left.executed_time_fs == right.executed_time_fs &&
           left.state == right.state;
  };
  while (replay.trace_.size() < trace_.size() ||
         (terminal_status_ != TerminalStatus::kRunning &&
          replay.terminal_status_ == TerminalStatus::kRunning)) {
    try {
      static_cast<void>(replay.step());
    } catch (const EngineError&) {
      if (replay.terminal_status_ != TerminalStatus::kZeno) {
        throw EngineError("checkpoint history replay failed");
      }
    }
    if (replay.trace_.size() > trace_.size()) {
      throw EngineError("checkpoint trace is not a scheduler-step prefix");
    }
    for (std::size_t index = 0; index < replay.trace_.size(); ++index) {
      if (!trace_entry_equal(replay.trace_[index], trace_[index])) {
        throw EngineError("checkpoint deterministic trace-order mismatch");
      }
    }
    if (replay.terminal_status_ != TerminalStatus::kRunning) {
      break;
    }
  }
  if (replay.state_digest() != state_digest()) {
    throw EngineError("checkpoint state is not a reachable replay prefix");
  }
}

std::optional<std::size_t> Engine::next_ready_pending_index() const {
  for (std::size_t index = 0; index < events_.size(); ++index) {
    if (states_.at(events_[index].key.event_id) == EventState::kPending &&
        dependencies_complete(events_[index])) {
      return index;
    }
  }
  return std::nullopt;
}

bool Engine::has_pending_events() const {
  return std::any_of(
      states_.begin(), states_.end(), [](const auto& item) {
        return item.second == EventState::kPending;
      });
}

bool Engine::dependencies_complete(const Event& event) const {
  return std::all_of(
      event.dependencies.begin(), event.dependencies.end(),
      [&](const std::string& dependency) {
        return states_.at(dependency) == EventState::kComplete;
      });
}

void Engine::mark_complete(const Event& event, const U128& executed_time) {
  if (processed_count_ >= config_.max_events) {
    terminal_status_ = TerminalStatus::kFailed;
    throw EngineError("EVENT_BUDGET_EXCEEDED");
  }
  account_transition();
  states_.at(event.key.event_id) = EventState::kComplete;
  trace_.push_back(
      {event.key.event_id, event.key.time_fs, executed_time,
       EventState::kComplete});
  ++processed_count_;
}

void Engine::account_transition() {
  if (last_event_time_fs_.has_value() &&
      *last_event_time_fs_ == global_time_fs_) {
    if (same_time_count_ >= config_.max_same_time_events) {
      terminal_status_ = TerminalStatus::kZeno;
      throw EngineError("ZENO_SAME_TIME_EVENT_BUDGET");
    }
    ++same_time_count_;
  } else {
    last_event_time_fs_ = global_time_fs_;
    same_time_count_ = 1;
  }
}

void Engine::acquire(const Event& event) {
  Resource& resource = resources_.at(event.resource_id);
  if (event.quantity <= resource.capacity - resource.occupancy) {
    const auto holder = resource.holders.find(event.owner_id);
    const U128 current_holder =
        holder == resource.holders.end() ? U128{0} : holder->second;
    const U128 next_occupancy = checked_add_u128(
        resource.occupancy, event.quantity, "resource occupancy");
    const U128 next_holder = checked_add_u128(
        current_holder, event.quantity, "resource holder quantity");
    mark_complete(event, global_time_fs_);
    resource.occupancy = next_occupancy;
    resource.holders[event.owner_id] = next_holder;
    return;
  }
  account_transition();
  states_.at(event.key.event_id) = EventState::kBlocked;
  resource.waiters.push_back(event.key.event_id);
  if (resource.arbitration == Arbitration::kPriority) {
    std::sort(
        resource.waiters.begin(), resource.waiters.end(),
        [&](const std::string& left, const std::string& right) {
          const auto event_by_id = [&](const std::string& id) -> const Event& {
            return *std::find_if(
                events_.begin(), events_.end(), [&](const Event& candidate) {
                  return candidate.key.event_id == id;
                });
          };
          return event_by_id(left).key < event_by_id(right).key;
        });
  }
  trace_.push_back(
      {event.key.event_id, event.key.time_fs, global_time_fs_,
       EventState::kBlocked});
}

void Engine::drain_waiters(Resource& resource) {
  while (!resource.waiters.empty()) {
    const std::string id = resource.waiters.front();
    const auto event_it = std::find_if(
        events_.begin(), events_.end(),
        [&](const Event& candidate) { return candidate.key.event_id == id; });
    if (event_it == events_.end()) {
      throw EngineError("resource waiter references missing event");
    }
    const Event& event = *event_it;
    if (event.quantity > resource.capacity - resource.occupancy) {
      return;
    }
    const auto holder = resource.holders.find(event.owner_id);
    const U128 current_holder =
        holder == resource.holders.end() ? U128{0} : holder->second;
    const U128 next_occupancy = checked_add_u128(
        resource.occupancy, event.quantity, "resource occupancy");
    const U128 next_holder = checked_add_u128(
        current_holder, event.quantity, "resource holder quantity");
    mark_complete(event, global_time_fs_);
    resource.waiters.erase(resource.waiters.begin());
    resource.occupancy = next_occupancy;
    resource.holders[event.owner_id] = next_holder;
  }
}

void Engine::release(const Event& event) {
  Resource& resource = resources_.at(event.resource_id);
  auto holder = resource.holders.find(event.owner_id);
  if (holder == resource.holders.end() || holder->second < event.quantity ||
      resource.occupancy < event.quantity) {
    throw EngineError("release underflow or wrong holder");
  }
  mark_complete(event, global_time_fs_);
  holder->second -= event.quantity;
  resource.occupancy -= event.quantity;
  if (holder->second == 0) {
    resource.holders.erase(holder);
  }
  drain_waiters(resource);
}

StepResult Engine::step() {
  if (terminal_status_ != TerminalStatus::kRunning) {
    throw EngineError("cannot step a terminal engine");
  }
  const auto index = next_ready_pending_index();
  if (!index.has_value()) {
    if (has_blocked_events() || has_pending_events()) {
      terminal_status_ = TerminalStatus::kDeadlock;
    } else {
      terminal_status_ = TerminalStatus::kQuiescent;
    }
    return {"", EventState::kComplete, global_time_fs_};
  }
  const Event& event = events_[*index];
  global_time_fs_ = std::max(global_time_fs_, event.key.time_fs);
  switch (event.action) {
    case Action::kAcquire:
      acquire(event);
      break;
    case Action::kRelease:
      release(event);
      break;
    case Action::kService:
      mark_complete(event, global_time_fs_);
      break;
    case Action::kTransferUnsupported:
      terminal_status_ = TerminalStatus::kFailed;
      throw EngineError("UNSUPPORTED_PHASE4_RESOURCE");
  }
  return {event.key.event_id, states_.at(event.key.event_id), global_time_fs_};
}

TerminalStatus Engine::run_until_quiescent() {
  while (terminal_status_ == TerminalStatus::kRunning) {
    static_cast<void>(step());
  }
  return terminal_status_;
}

TerminalStatus Engine::run_until_time(const U128& inclusive_limit) {
  while (terminal_status_ == TerminalStatus::kRunning) {
    const auto index = next_ready_pending_index();
    if (!index.has_value() || events_[*index].key.time_fs > inclusive_limit) {
      break;
    }
    static_cast<void>(step());
  }
  return terminal_status_;
}

bool Engine::has_blocked_events() const {
  return std::any_of(
      states_.begin(), states_.end(), [](const auto& item) {
        return item.second == EventState::kBlocked;
      });
}

std::string Engine::diagnose_deadlock() const {
  if (!has_blocked_events()) {
    return "NO_DEADLOCK";
  }
  std::map<std::string, std::vector<std::string>> wait_for;
  std::size_t edge_count = 0;
  for (const auto& [id, resource] : resources_) {
    static_cast<void>(id);
    for (const auto& waiter_id : resource.waiters) {
      const auto waiter = std::find_if(
          events_.begin(), events_.end(), [&](const Event& event) {
            return event.key.event_id == waiter_id;
          });
      if (waiter == events_.end()) {
        throw EngineError("deadlock waiter references missing event");
      }
      for (const auto& [holder, quantity] : resource.holders) {
        if (quantity != 0) {
          if (edge_count >= config_.max_wait_for_nodes) {
            throw EngineError("WAIT_FOR_GRAPH_LIMIT_EXCEEDED");
          }
          wait_for[waiter->owner_id].push_back(holder);
          wait_for.try_emplace(holder);
          ++edge_count;
          if (wait_for.size() > config_.max_wait_for_nodes) {
            throw EngineError("WAIT_FOR_GRAPH_LIMIT_EXCEEDED");
          }
        }
      }
    }
  }
  for (auto& [node, targets] : wait_for) {
    static_cast<void>(node);
    std::sort(targets.begin(), targets.end());
    targets.erase(std::unique(targets.begin(), targets.end()), targets.end());
  }
  if (wait_for.size() > config_.max_wait_for_nodes ||
      edge_count > config_.max_wait_for_nodes) {
    throw EngineError("WAIT_FOR_GRAPH_LIMIT_EXCEEDED");
  }
  std::map<std::string, int> color;
  std::vector<std::string> cycle;
  struct Frame {
    std::string node;
    std::size_t next_target;
  };
  for (const auto& [node, targets] : wait_for) {
    static_cast<void>(targets);
    if (color[node] != 0) {
      continue;
    }
    std::vector<Frame> frames{{node, 0}};
    std::vector<std::string> path{node};
    color[node] = 1;
    while (!frames.empty() && cycle.empty()) {
      Frame& frame = frames.back();
      const auto& neighbors = wait_for.at(frame.node);
      if (frame.next_target == neighbors.size()) {
        color[frame.node] = 2;
        frames.pop_back();
        path.pop_back();
        continue;
      }
      const std::string target = neighbors[frame.next_target++];
      if (color[target] == 0) {
        color[target] = 1;
        frames.push_back({target, 0});
        path.push_back(target);
      } else if (color[target] == 1) {
        const auto begin = std::find(path.begin(), path.end(), target);
        cycle.assign(begin, path.end());
        cycle.push_back(target);
      }
    }
    if (!cycle.empty()) {
      std::ostringstream stream;
      stream << "WAIT_FOR_CYCLE:";
      for (std::size_t index = 0; index < cycle.size(); ++index) {
        if (index != 0) {
          stream << "->";
        }
        stream << cycle[index];
      }
      return stream.str();
    }
  }
  std::ostringstream stream;
  stream << "RESOURCE_STARVATION:";
  for (const auto& [id, resource] : resources_) {
    if (!resource.waiters.empty()) {
      stream << id << "[";
      for (std::size_t index = 0; index < resource.waiters.size(); ++index) {
        if (index != 0) {
          stream << ",";
        }
        stream << resource.waiters[index];
      }
      stream << "]";
    }
  }
  return stream.str();
}

namespace {

std::string canonical_checkpoint_body(const Checkpoint& value) {
  std::string output;
  const auto put = [&](const std::string& field) {
    append_field(output, field);
  };
  put("moe-phase3-checkpoint-v1");
  put(value.schema_version);
  put(std::to_string(value.config.max_events));
  put(std::to_string(value.config.max_same_time_events));
  put(std::to_string(value.config.max_wait_for_nodes));
  put(value.config.phase2_ledger_sha256);
  put(value.config.canonical_bundle_semantic_root);
  put(value.config.engine_build_sha256);
  put(value.config.engine_profile_sha256);
  put(value.config.checkpoint_schema_sha256);
  put(to_decimal(value.global_time_fs));
  put(value.last_event_time_fs.has_value() ? "1" : "0");
  if (value.last_event_time_fs.has_value()) {
    put(to_decimal(*value.last_event_time_fs));
  }
  put(std::to_string(value.same_time_count));
  put(std::to_string(value.processed_count));
  put(std::to_string(static_cast<int>(value.terminal_status)));

  put(std::to_string(value.events.size()));
  for (const auto& event : value.events) {
    put(to_decimal(event.key.time_fs));
    put(std::to_string(event.key.event_priority));
    put(event.key.request_id.has_value() ? "1" : "0");
    if (event.key.request_id.has_value()) {
      put(*event.key.request_id);
    }
    put(event.key.token_index.has_value() ? "1" : "0");
    if (event.key.token_index.has_value()) {
      put(std::to_string(*event.key.token_index));
    }
    put(event.key.layer_index.has_value() ? "1" : "0");
    if (event.key.layer_index.has_value()) {
      put(std::to_string(*event.key.layer_index));
    }
    put(event.key.component_id);
    put(event.key.event_id);
    put(std::to_string(event.dependencies.size()));
    for (const auto& dependency : event.dependencies) {
      put(dependency);
    }
    put(std::to_string(static_cast<int>(event.action)));
    put(event.resource_id);
    put(event.owner_id);
    put(to_decimal(event.quantity));
    put(to_decimal(event.service_demand));
  }

  put(std::to_string(value.states.size()));
  for (const auto& [id, state] : value.states) {
    put(id);
    put(std::to_string(static_cast<int>(state)));
  }
  put(std::to_string(value.clocks.size()));
  for (const auto& [id, clock] : value.clocks) {
    put(id);
    put(std::to_string(clock.frequency_numerator_hz));
    put(std::to_string(clock.frequency_denominator_hz));
    put(to_decimal(clock.phase_offset_fs));
    put(std::to_string(clock.local_cycle));
    put(std::to_string(clock.fractional_remainder));
  }
  put(std::to_string(value.resources.size()));
  for (const auto& [id, resource] : value.resources) {
    put(id);
    put(std::to_string(static_cast<int>(resource.kind)));
    put(std::to_string(static_cast<int>(resource.arbitration)));
    put(to_decimal(resource.capacity));
    put(to_decimal(resource.occupancy));
    put(std::to_string(resource.holders.size()));
    for (const auto& [owner, quantity] : resource.holders) {
      put(owner);
      put(to_decimal(quantity));
    }
    put(std::to_string(resource.waiters.size()));
    for (const auto& waiter : resource.waiters) {
      put(waiter);
    }
  }
  put(std::to_string(value.trace.size()));
  for (const auto& item : value.trace) {
    put(item.event_id);
    put(to_decimal(item.scheduled_time_fs));
    put(to_decimal(item.executed_time_fs));
    put(std::to_string(static_cast<int>(item.state)));
  }
  return output;
}

}  // namespace

std::string Engine::state_digest() const {
  const Checkpoint value{
      "checkpoint-v1",
      config_,
      global_time_fs_,
      last_event_time_fs_,
      same_time_count_,
      processed_count_,
      terminal_status_,
      events_,
      states_,
      clocks_,
      resources_,
      trace_,
      ""};
  return checkpoint_digest(value);
}

std::string Engine::checkpoint_digest(const Checkpoint& checkpoint) {
  return sha256_bytes(canonical_checkpoint_body(checkpoint));
}

Checkpoint Engine::checkpoint() const {
  Checkpoint value{
      "checkpoint-v1",
      config_,
      global_time_fs_,
      last_event_time_fs_,
      same_time_count_,
      processed_count_,
      terminal_status_,
      events_,
      states_,
      clocks_,
      resources_,
      trace_,
      ""};
  value.state_digest = checkpoint_digest(value);
  return value;
}

std::string Engine::serialize_checkpoint() const {
  const Checkpoint value = checkpoint();
  std::string output = canonical_checkpoint_body(value);
  const auto put = [&](const std::string& field) {
    append_field(output, field);
  };
  put(value.state_digest);
  return output;
}

Engine Engine::restore_serialized(
    const std::string& bytes,
    const EngineConfig& expected_config) {
  FieldReader reader(bytes);
  if (reader.next() != "moe-phase3-checkpoint-v1") {
    throw EngineError("checkpoint wire magic mismatch");
  }
  Checkpoint value;
  value.schema_version = reader.next();
  value.config.max_events =
      parse_u64_field(reader.next(), "max events");
  value.config.max_same_time_events =
      parse_u64_field(reader.next(), "same-time limit");
  value.config.max_wait_for_nodes =
      parse_u64_field(reader.next(), "wait-for node limit");
  value.config.phase2_ledger_sha256 = reader.next();
  value.config.canonical_bundle_semantic_root = reader.next();
  value.config.engine_build_sha256 = reader.next();
  value.config.engine_profile_sha256 = reader.next();
  value.config.checkpoint_schema_sha256 = reader.next();
  value.global_time_fs = parse_u128(reader.next());
  const std::string has_last = reader.next();
  if (has_last == "1") {
    value.last_event_time_fs = parse_u128(reader.next());
  } else if (has_last != "0") {
    throw EngineError("invalid optional last-event-time flag");
  }
  value.same_time_count =
      parse_u64_field(reader.next(), "same-time count");
  value.processed_count =
      parse_u64_field(reader.next(), "processed count");
  value.terminal_status = parse_enum_field<TerminalStatus>(
      reader.next(), static_cast<int>(TerminalStatus::kFailed),
      "terminal status");

  const std::size_t event_count =
      parse_size_field(reader.next(), "event count");
  if (event_count > expected_config.max_events) {
    throw EngineError("checkpoint event count exceeds configured limit");
  }
  value.events.reserve(event_count);
  for (std::size_t index = 0; index < event_count; ++index) {
    Event event;
    event.key.time_fs = parse_u128(reader.next());
    event.key.event_priority =
        parse_u32_field(reader.next(), "event priority");
    const std::string has_request = reader.next();
    if (has_request == "1") {
      event.key.request_id = reader.next();
    } else if (has_request != "0") {
      throw EngineError("invalid optional request flag");
    }
    const std::string has_token = reader.next();
    if (has_token == "1") {
      event.key.token_index =
          parse_u64_field(reader.next(), "token index");
    } else if (has_token != "0") {
      throw EngineError("invalid optional token flag");
    }
    const std::string has_layer = reader.next();
    if (has_layer == "1") {
      event.key.layer_index =
          parse_u32_field(reader.next(), "layer index");
    } else if (has_layer != "0") {
      throw EngineError("invalid optional layer flag");
    }
    event.key.component_id = reader.next();
    event.key.event_id = reader.next();
    const std::size_t dependency_count =
        parse_size_field(reader.next(), "dependency count");
    if (dependency_count > expected_config.max_events) {
      throw EngineError(
          "checkpoint dependency count exceeds configured limit");
    }
    event.dependencies.reserve(dependency_count);
    for (std::size_t dependency = 0; dependency < dependency_count;
         ++dependency) {
      event.dependencies.push_back(reader.next());
    }
    event.action = parse_enum_field<Action>(
        reader.next(), static_cast<int>(Action::kTransferUnsupported),
        "event action");
    event.resource_id = reader.next();
    event.owner_id = reader.next();
    event.quantity = parse_u128(reader.next());
    event.service_demand = parse_u128(reader.next());
    value.events.push_back(std::move(event));
  }

  const std::size_t state_count =
      parse_size_field(reader.next(), "state count");
  if (state_count != event_count) {
    throw EngineError("checkpoint event/state count mismatch");
  }
  for (std::size_t index = 0; index < state_count; ++index) {
    const std::string id = reader.next();
    const EventState state = parse_enum_field<EventState>(
        reader.next(), static_cast<int>(EventState::kFailed),
        "event state");
    if (!value.states.emplace(id, state).second) {
      throw EngineError("duplicate checkpoint event state");
    }
  }
  const std::size_t clock_count =
      parse_size_field(reader.next(), "clock count");
  if (clock_count > expected_config.max_events) {
    throw EngineError("checkpoint clock count exceeds configured limit");
  }
  for (std::size_t index = 0; index < clock_count; ++index) {
    Clock clock;
    clock.clock_id = reader.next();
    clock.frequency_numerator_hz =
        parse_u64_field(reader.next(), "frequency numerator");
    clock.frequency_denominator_hz =
        parse_u64_field(reader.next(), "frequency denominator");
    clock.phase_offset_fs = parse_u128(reader.next());
    clock.local_cycle =
        parse_u64_field(reader.next(), "local cycle");
    clock.fractional_remainder =
        parse_u64_field(reader.next(), "fractional remainder");
    if (!value.clocks.emplace(clock.clock_id, clock).second) {
      throw EngineError("duplicate checkpoint clock");
    }
  }
  const std::size_t resource_count =
      parse_size_field(reader.next(), "resource count");
  if (resource_count > expected_config.max_events) {
    throw EngineError("checkpoint resource count exceeds configured limit");
  }
  std::size_t total_holder_count = 0;
  std::size_t total_waiter_count = 0;
  for (std::size_t index = 0; index < resource_count; ++index) {
    Resource resource;
    resource.resource_id = reader.next();
    resource.kind = parse_enum_field<ResourceKind>(
        reader.next(), static_cast<int>(ResourceKind::kUnsupportedPhase4),
        "resource kind");
    resource.arbitration = parse_enum_field<Arbitration>(
        reader.next(),
        static_cast<int>(Arbitration::kRoundRobinUnsupported),
        "resource arbitration");
    resource.capacity = parse_u128(reader.next());
    resource.occupancy = parse_u128(reader.next());
    const std::size_t holder_count =
        parse_size_field(reader.next(), "holder count");
    if (holder_count > expected_config.max_events) {
      throw EngineError("checkpoint holder count exceeds configured limit");
    }
    if (holder_count > expected_config.max_events - total_holder_count) {
      throw EngineError(
          "checkpoint total holder count exceeds configured limit");
    }
    total_holder_count += holder_count;
    for (std::size_t holder = 0; holder < holder_count; ++holder) {
      const std::string owner = reader.next();
      if (!resource.holders.emplace(owner, parse_u128(reader.next())).second) {
        throw EngineError("duplicate checkpoint holder");
      }
    }
    const std::size_t waiter_count =
        parse_size_field(reader.next(), "waiter count");
    if (waiter_count > expected_config.max_events) {
      throw EngineError("checkpoint waiter count exceeds configured limit");
    }
    if (waiter_count > expected_config.max_events - total_waiter_count) {
      throw EngineError(
          "checkpoint total waiter count exceeds configured limit");
    }
    total_waiter_count += waiter_count;
    resource.waiters.reserve(waiter_count);
    for (std::size_t waiter = 0; waiter < waiter_count; ++waiter) {
      resource.waiters.push_back(reader.next());
    }
    if (!value.resources.emplace(resource.resource_id, resource).second) {
      throw EngineError("duplicate checkpoint resource");
    }
  }
  const std::size_t trace_count =
      parse_size_field(reader.next(), "trace count");
  if (cpp_int{trace_count} > cpp_int{expected_config.max_events} * 2) {
    throw EngineError("checkpoint trace count exceeds configured limit");
  }
  value.trace.reserve(trace_count);
  for (std::size_t index = 0; index < trace_count; ++index) {
    value.trace.push_back(
        {reader.next(),
         parse_u128(reader.next()),
         parse_u128(reader.next()),
         parse_enum_field<EventState>(
             reader.next(), static_cast<int>(EventState::kFailed),
             "trace event state")});
  }
  value.state_digest = reader.next();
  reader.require_end();
  return restore(value, expected_config);
}

Engine Engine::restore(
    const Checkpoint& checkpoint,
    const EngineConfig& expected_config) {
  if (checkpoint.schema_version != "checkpoint-v1" ||
      !same_config(checkpoint.config, expected_config)) {
    throw EngineError("checkpoint contract hash or schema mismatch");
  }
  std::vector<Resource> resources;
  resources.reserve(checkpoint.resources.size());
  for (const auto& [id, resource] : checkpoint.resources) {
    static_cast<void>(id);
    resources.push_back(resource);
  }
  std::vector<Clock> clocks;
  clocks.reserve(checkpoint.clocks.size());
  for (const auto& [id, clock] : checkpoint.clocks) {
    static_cast<void>(id);
    clocks.push_back(clock);
  }
  Engine engine(
      checkpoint.events, std::move(resources), checkpoint.config,
      std::move(clocks), RestoreConstructionTag{});
  engine.global_time_fs_ = checkpoint.global_time_fs;
  engine.last_event_time_fs_ = checkpoint.last_event_time_fs;
  engine.same_time_count_ = checkpoint.same_time_count;
  engine.processed_count_ = checkpoint.processed_count;
  engine.terminal_status_ = checkpoint.terminal_status;
  engine.states_ = checkpoint.states;
  engine.trace_ = checkpoint.trace;
  if (!is_sha256(checkpoint.state_digest)) {
    throw EngineError("checkpoint state digest is not SHA-256");
  }
  if (engine.state_digest() != checkpoint.state_digest) {
    throw EngineError("checkpoint state digest mismatch");
  }
  engine.validate_runtime_state();
  return engine;
}

}  // namespace moe_sim
