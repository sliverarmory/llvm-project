#include <ctype.h>
#include <curl/curl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

enum { MaxBodyBytes = 1024 * 1024 };

typedef struct {
  char *data;
  size_t length;
} Response;

typedef struct {
  char title[128];
  size_t links;
  size_t path_bytes;
} Summary;

static size_t append_response(char *bytes, size_t size, size_t count,
                              void *context) {
  Response *response = (Response *)context;
  if (size && count > SIZE_MAX / size)
    return 0;
  size_t length = size * count;
  if (length > MaxBodyBytes - response->length)
    return 0;
  char *grown = (char *)realloc(response->data, response->length + length + 1);
  if (!grown)
    return 0;
  response->data = grown;
  memcpy(grown + response->length, bytes, length);
  response->length += length;
  grown[response->length] = '\0';
  return length;
}

static int matches(const char *text, size_t available, const char *word) {
  size_t length = strlen(word);
  if (available < length)
    return 0;
  for (size_t i = 0; i < length; ++i)
    if (tolower((unsigned char)text[i]) != tolower((unsigned char)word[i]))
      return 0;
  return 1;
}

static int read_title(const char *html, size_t length, Summary *summary) {
  for (size_t i = 0; i + 6 < length; ++i) {
    if (!matches(html + i, length - i, "<title"))
      continue;
    if (html[i + 6] != '>' && !isspace((unsigned char)html[i + 6]))
      continue;
    size_t begin = i + 6;
    while (begin < length && html[begin] != '>')
      ++begin;
    if (begin == length)
      return 0;
    ++begin;
    while (begin < length && isspace((unsigned char)html[begin]))
      ++begin;
    size_t end = begin;
    while (end < length && html[end] != '<')
      ++end;
    while (end > begin && isspace((unsigned char)html[end - 1]))
      --end;
    if (end == begin || end - begin >= sizeof(summary->title))
      return 0;
    memcpy(summary->title, html + begin, end - begin);
    summary->title[end - begin] = '\0';
    return 1;
  }
  return 0;
}

static size_t href_path_length(const char *href, size_t length) {
  size_t begin = 0;
  if (matches(href, length, "https://") || matches(href, length, "http://")) {
    begin = href[4] == 's' || href[4] == 'S' ? 8 : 7;
    while (begin < length && href[begin] != '/' && href[begin] != '?' &&
           href[begin] != '#')
      ++begin;
    if (begin == length || href[begin] != '/')
      return 0;
  } else if (length >= 2 && href[0] == '/' && href[1] == '/') {
    begin = 2;
    while (begin < length && href[begin] != '/' && href[begin] != '?' &&
           href[begin] != '#')
      ++begin;
    if (begin == length || href[begin] != '/')
      return 0;
  } else if (length && href[0] == '#') {
    return 0;
  }
  size_t end = begin;
  while (end < length && href[end] != '?' && href[end] != '#')
    ++end;
  return end - begin;
}

// The harness checks this C ABI symbol in emitted IR as well as its behavior.
__attribute__((noinline)) int transform_target(const char *html, size_t length,
                                               Summary *summary) {
  memset(summary, 0, sizeof(*summary));
  if (!read_title(html, length, summary))
    return 0;

  for (size_t i = 0; i + 2 < length; ++i) {
    if (html[i] != '<' || tolower((unsigned char)html[i + 1]) != 'a' ||
        (html[i + 2] != '>' && !isspace((unsigned char)html[i + 2])))
      continue;
    size_t cursor = i + 2;
    while (cursor < length && html[cursor] != '>') {
      while (cursor < length && isspace((unsigned char)html[cursor]))
        ++cursor;
      size_t name = cursor;
      while (cursor < length && (isalnum((unsigned char)html[cursor]) ||
                                 html[cursor] == '-' || html[cursor] == '_'))
        ++cursor;
      size_t name_length = cursor - name;
      if (!name_length) {
        ++cursor;
        continue;
      }
      while (cursor < length && isspace((unsigned char)html[cursor]))
        ++cursor;
      if (cursor == length || html[cursor] != '=')
        continue;
      ++cursor;
      while (cursor < length && isspace((unsigned char)html[cursor]))
        ++cursor;
      if (cursor == length)
        break;
      char quote = 0;
      if (html[cursor] == '"' || html[cursor] == '\'')
        quote = html[cursor++];
      size_t value = cursor;
      while (cursor < length &&
             (quote ? html[cursor] != quote
                    : html[cursor] != '>' &&
                          !isspace((unsigned char)html[cursor])))
        ++cursor;
      size_t value_length = cursor - value;
      if (quote && cursor < length)
        ++cursor;
      if (name_length == 4 && matches(html + name, name_length, "href")) {
        size_t path_length = href_path_length(html + value, value_length);
        if (path_length) {
          if (summary->path_bytes > SIZE_MAX - path_length)
            return 0;
          ++summary->links;
          summary->path_bytes += path_length;
        }
      }
    }
    i = cursor;
  }
  return 1;
}

__attribute__((objc_root_class))
@interface HTTPFixture
+ (int)fetch:(const char *)url
      withCA:(const char *)ca
        into:(Response *)response;
+ (int)printSummary:(const Response *)response;
@end

@implementation HTTPFixture
+ (int)fetch:(const char *)url
      withCA:(const char *)ca
        into:(Response *)response {
  if (strncmp(url, "https://", 8) != 0)
    return 0;
  CURL *curl = curl_easy_init();
  if (!curl)
    return 0;
  int configured =
      curl_easy_setopt(curl, CURLOPT_URL, url) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_CAINFO, ca) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 5L) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_USERAGENT,
                       "LLVM-OBFUSCATION-HTTP-CLIENT-MARKER") == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, append_response) ==
          CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_WRITEDATA, response) == CURLE_OK;
#if LIBCURL_VERSION_NUM >= 0x075500
  configured =
      configured &&
      curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "https") == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS_STR, "https") == CURLE_OK;
#else
  configured =
      configured &&
      curl_easy_setopt(curl, CURLOPT_PROTOCOLS, CURLPROTO_HTTPS) == CURLE_OK &&
      curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS, CURLPROTO_HTTPS) ==
          CURLE_OK;
#endif
  CURLcode result = configured ? curl_easy_perform(curl) : CURLE_FAILED_INIT;
  long status = 0;
  if (result == CURLE_OK)
    result = curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
  curl_easy_cleanup(curl);
  return result == CURLE_OK && status == 200;
}

+ (int)printSummary:(const Response *)response {
  Summary summary;
  if (!transform_target(response->data, response->length, &summary))
    return 0;
  printf("title=%s;links=%zu;path_bytes=%zu\n", summary.title, summary.links,
         summary.path_bytes);
  return 1;
}
@end

int main(int argc, char **argv) {
  if (argc != 3 || curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK)
    return 2;
  Response response = {0};
  int success = [HTTPFixture fetch:argv[1] withCA:argv[2] into:&response] &&
                [HTTPFixture printSummary:&response];
  free(response.data);
  curl_global_cleanup();
  if (!success)
    fprintf(stderr, "HTTPS fetch or HTML parsing failed\n");
  return success ? 0 : 1;
}
