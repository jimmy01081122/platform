#include <openssl/sha.h>

#include <array>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

std::array<unsigned char, SHA256_DIGEST_LENGTH> sha256_bytes(
    const std::vector<unsigned char>& bytes) {
  std::array<unsigned char, SHA256_DIGEST_LENGTH> output{};
  SHA256(bytes.data(), bytes.size(), output.data());
  return output;
}

std::array<unsigned char, SHA256_DIGEST_LENGTH> sha256_text(
    const std::string& text) {
  return sha256_bytes(
      std::vector<unsigned char>(text.begin(), text.end()));
}

std::string hex(
    const std::array<unsigned char, SHA256_DIGEST_LENGTH>& bytes) {
  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (unsigned char byte : bytes) {
    output << std::setw(2) << static_cast<unsigned int>(byte);
  }
  return output.str();
}

void append(std::vector<unsigned char>& output, const std::string& text) {
  output.insert(output.end(), text.begin(), text.end());
}

void append(
    std::vector<unsigned char>& output,
    const std::array<unsigned char, SHA256_DIGEST_LENGTH>& bytes) {
  output.insert(output.end(), bytes.begin(), bytes.end());
}

}  // namespace

int main() {
  const std::string descriptor =
      R"({"field_order":["request_id","token_index","label"],"logical_types":["utf8","uint64","utf8"],"nullability":[false,false,true],"primary_key":["request_id","token_index"],"schema_id":"phase0-golden-row","schema_version":"1","semantic_metadata":{"unicode":"NFC"},"units":[null,"token",null]})";
  const std::vector<std::string> rows = {
      R"({"label":"alpha","request_id":"request-01","token_index":0})",
      R"({"label":null,"request_id":"réquest-02","token_index":1})",
  };
  const auto fingerprint = sha256_text(descriptor);
  if (hex(fingerprint) !=
      "1ac30d893b81c9fd30a1c356350ab1f74f29afc22d206305139c8e4fc6b17386") {
    throw std::runtime_error("schema fingerprint mismatch");
  }

  std::vector<std::array<unsigned char, SHA256_DIGEST_LENGTH>> row_hashes;
  for (const auto& row : rows) {
    std::vector<unsigned char> input;
    append(input, std::string("moe-row-v1\0", 11));
    append(input, fingerprint);
    append(input, std::string("\0", 1));
    append(input, row);
    row_hashes.push_back(sha256_bytes(input));
  }
  if (hex(row_hashes.at(0)) !=
          "5a10e5c0b8b66693eaf5870191893adbb46838ab643b2afb5479e6bffc9e7a5b" ||
      hex(row_hashes.at(1)) !=
          "2b69d242c2c169a4d9e9a4d98d396e5ab537ea5c6906abce4b54f385c9f30162") {
    throw std::runtime_error("row semantic hash mismatch");
  }

  std::vector<unsigned char> aggregate_input;
  append(aggregate_input, std::string("moe-dataset-v1\0", 15));
  append(aggregate_input, fingerprint);
  for (const auto& row_hash : row_hashes) {
    append(aggregate_input, row_hash);
  }
  const auto aggregate = sha256_bytes(aggregate_input);
  if (hex(aggregate) !=
      "f1ae2feca59dc3b3805836d658f3403573a1613ba83d7b7159c62180a9646390") {
    throw std::runtime_error("dataset semantic hash mismatch");
  }
  std::cout << "CPP_SEMANTIC_HASH_GOLDEN: PASS\n";
  return 0;
}
