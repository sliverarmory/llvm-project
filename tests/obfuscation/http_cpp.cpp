#include <curl/curl.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <string_view>

namespace {
constexpr size_t MaxPageBytes = 1024U * 1024U;

size_t receive_body(char *data, size_t size, size_t count,
                    void *opaque) noexcept {
  auto &body = *static_cast<std::string *>(opaque);
  if (size != 0 && count > SIZE_MAX / size)
    return 0;
  const size_t bytes = size * count;
  if (bytes > MaxPageBytes - body.size())
    return 0;
  try {
    body.append(data, bytes);
  } catch (...) {
    return 0;
  }
  return bytes;
}

size_t href_path_bytes(std::string_view href) {
  if (href.rfind("https://", 0) == 0 || href.rfind("http://", 0) == 0) {
    const size_t authority = href.find("://") + 3;
    const size_t slash = href.find('/', authority);
    if (slash == std::string_view::npos)
      return 1; // The path of an absolute URL without a slash is "/".
    href.remove_prefix(slash);
  }
  if (href.empty() || href.front() == '#' || href.front() == '?')
    return 0;
  const size_t end = href.find_first_of("?#");
  return end == std::string_view::npos ? href.size() : end;
}

struct CurlGlobal {
  CurlGlobal() : status(curl_global_init(CURL_GLOBAL_DEFAULT)) {}
  ~CurlGlobal() {
    if (status == CURLE_OK)
      curl_global_cleanup();
  }
  CURLcode status;
};
} // namespace

// C linkage and no inlining keep this parsing routine visible in emitted IR.
extern "C" __attribute__((noinline)) int
transform_target(const char *html, size_t length, char *title,
                 size_t title_cap, size_t *links, size_t *path_bytes,
                 uint32_t *checksum) {
  const std::string_view page(html, length);
  const size_t open = page.find("<title>");
  if (open == std::string_view::npos)
    return -1;
  const size_t title_begin = open + std::string_view("<title>").size();
  const size_t title_end = page.find("</title>", title_begin);
  if (title_end == std::string_view::npos || title_end - title_begin >= title_cap)
    return -1;
  const std::string_view parsed_title =
      page.substr(title_begin, title_end - title_begin);
  std::memcpy(title, parsed_title.data(), parsed_title.size());
  title[parsed_title.size()] = '\0';

  uint32_t hash = 2166136261U;
  for (const unsigned char byte : parsed_title) {
    hash ^= byte;
    hash *= 16777619U;
  }

  size_t found_links = 0;
  size_t total_path_bytes = 0;
  size_t cursor = title_end + std::string_view("</title>").size();
  while ((cursor = page.find("href=\"", cursor)) != std::string_view::npos) {
    const size_t value_begin = cursor + std::string_view("href=\"").size();
    const size_t value_end = page.find('"', value_begin);
    if (value_end == std::string_view::npos)
      return -1;
    const std::string_view href = page.substr(value_begin, value_end - value_begin);
    const size_t path_length = href_path_bytes(href);
    if (path_length != 0) {
      if (total_path_bytes > SIZE_MAX - path_length)
        return -1;
      ++found_links;
      total_path_bytes += path_length;
      for (const unsigned char byte : href) {
        hash = (hash << 5) | (hash >> 27);
        hash ^= byte;
      }
      if (hash & 1U)
        hash ^= 0x9e3779b9U;
      else
        hash += 0x7f4a7c15U;
    }
    cursor = value_end + 1;
  }

  *links = found_links;
  *path_bytes = total_path_bytes;
  *checksum = hash;
  return found_links ? 0 : -1;
}

int main(int argc, char **argv) {
  if (argc != 3 || std::strncmp(argv[1], "https://", 8) != 0) {
    std::fprintf(stderr, "usage: %s https://URL CA_CERT\n", argv[0]);
    return 2;
  }
  CurlGlobal global;
  if (global.status != CURLE_OK)
    return 1;
  std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(),
                                                           curl_easy_cleanup);
  if (!curl) {
    std::fputs("curl initialization failed\n", stderr);
    return 1;
  }

  std::string body;
  CURL *handle = curl.get();
  if (curl_easy_setopt(handle, CURLOPT_URL, argv[1]) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_CAINFO, argv[2]) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_USERAGENT,
                       "LLVM-OBFUSCATION-HTTP-CLIENT-MARKER") != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_PROTOCOLS_STR, "https") != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_REDIR_PROTOCOLS_STR, "https") != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_FOLLOWLOCATION, 1L) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_MAXREDIRS, 5L) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_CONNECTTIMEOUT, 5L) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_TIMEOUT, 15L) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_WRITEFUNCTION, receive_body) != CURLE_OK ||
      curl_easy_setopt(handle, CURLOPT_WRITEDATA, &body) != CURLE_OK) {
    std::fputs("curl setup failed\n", stderr);
    return 1;
  }

  long status = 0;
  if (curl_easy_perform(handle) != CURLE_OK ||
      curl_easy_getinfo(handle, CURLINFO_RESPONSE_CODE, &status) != CURLE_OK ||
      status != 200) {
    std::fputs("HTTPS request did not return 200\n", stderr);
    return 1;
  }

  char title[128];
  size_t links = 0;
  size_t path_bytes = 0;
  uint32_t checksum = 0;
  if (transform_target(body.data(), body.size(), title, sizeof(title), &links,
                       &path_bytes, &checksum) != 0 ||
      checksum == 0) {
    std::fputs("HTML parse failed\n", stderr);
    return 1;
  }
  std::printf("title=%s;links=%zu;path_bytes=%zu\n", title, links,
              path_bytes);
  return 0;
}
