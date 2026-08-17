#include <openssl/sha.h>

#include <array>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using Digest = std::array<unsigned char, SHA256_DIGEST_LENGTH>;

Digest sha256(const std::vector<unsigned char>& input) {
  Digest output{};
  SHA256(input.data(), input.size(), output.data());
  return output;
}

Digest sha256(const std::string& input) {
  return sha256(std::vector<unsigned char>(input.begin(), input.end()));
}

void append(std::vector<unsigned char>& output, const std::string& value) {
  output.insert(output.end(), value.begin(), value.end());
}

void append(std::vector<unsigned char>& output, const Digest& value) {
  output.insert(output.end(), value.begin(), value.end());
}

std::string hex(const Digest& value) {
  std::ostringstream output;
  output << std::hex << std::setfill('0');
  for (const auto byte : value) {
    output << std::setw(2) << static_cast<unsigned int>(byte);
  }
  return output.str();
}

std::string read_file(const std::string& path) {
  std::ifstream input(path, std::ios::binary);
  if (!input) throw std::runtime_error("cannot open input");
  return std::string(
      std::istreambuf_iterator<char>(input),
      std::istreambuf_iterator<char>());
}
}  // namespace

int main(int argc, char** argv) {
  if (argc != 3) {
    std::cerr << "usage: semantic_hash_golden DESCRIPTOR ROWS_JSONL\n";
    return 2;
  }
  const auto descriptor = read_file(argv[1]);
  std::ifstream rows(argv[2]);
  if (!rows) throw std::runtime_error("cannot open rows");
  const auto fingerprint = sha256(descriptor);
  std::vector<Digest> row_hashes;
  std::string row;
  while (std::getline(rows, row)) {
    if (row.empty()) throw std::runtime_error("blank canonical row");
    std::vector<unsigned char> input;
    append(input, std::string("moe-row-v1\0", 11));
    append(input, fingerprint);
    append(input, std::string("\0", 1));
    append(input, row);
    row_hashes.push_back(sha256(input));
  }
  if (row_hashes.empty()) throw std::runtime_error("empty row set");
  std::vector<unsigned char> aggregate_input;
  append(aggregate_input, std::string("moe-dataset-v1\0", 15));
  append(aggregate_input, fingerprint);
  for (const auto& digest : row_hashes) append(aggregate_input, digest);
  std::cout << hex(sha256(aggregate_input)) << "\n";
  return 0;
}
