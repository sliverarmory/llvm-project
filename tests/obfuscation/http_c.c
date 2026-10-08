#include <curl/curl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_PAGE_BYTES (1024U * 1024U)

struct response {
  char *data;
  size_t length;
};

static size_t receive_body(char *data, size_t size, size_t count, void *opaque) {
  struct response *body = (struct response *)opaque;
  if (size != 0 && count > SIZE_MAX / size)
    return 0;
  size_t bytes = size * count;
  if (bytes > MAX_PAGE_BYTES - body->length)
    return 0;

  char *grown = (char *)realloc(body->data, body->length + bytes + 1);
  if (!grown)
    return 0;
  body->data = grown;
  memcpy(body->data + body->length, data, bytes);
  body->length += bytes;
  body->data[body->length] = '\0';
  return bytes;
}

static const char *find_bytes(const char *begin, const char *end,
                              const char *needle, size_t needle_length) {
  if (needle_length == 0)
    return begin;
  for (const char *p = begin; p <= end && (size_t)(end - p) >= needle_length;
       ++p) {
    if (memcmp(p, needle, needle_length) == 0)
      return p;
  }
  return NULL;
}

static size_t href_path_bytes(const char *begin, const char *end) {
  const char *path = begin;
  if ((size_t)(end - begin) >= 8 && memcmp(begin, "https://", 8) == 0) {
    path = begin + 8;
    while (path < end && *path != '/')
      ++path;
    if (path == end)
      return 1; // An absolute URL without an explicit path means "/".
  } else if ((size_t)(end - begin) >= 7 &&
             memcmp(begin, "http://", 7) == 0) {
    path = begin + 7;
    while (path < end && *path != '/')
      ++path;
    if (path == end)
      return 1;
  }
  if (path == end || *path == '#' || *path == '?')
    return 0;
  const char *limit = path;
  while (limit < end && *limit != '#' && *limit != '?')
    ++limit;
  return (size_t)(limit - path);
}

// Kept out of line so the CI check can inspect the transformed IR body.
__attribute__((noinline)) int transform_target(const char *html, size_t length,
                                                char *title, size_t title_cap,
                                                size_t *links,
                                                size_t *path_bytes,
                                                uint32_t *checksum) {
  const char *end = html + length;
  const char *title_begin = find_bytes(html, end, "<title>", 7);
  if (!title_begin)
    return -1;
  title_begin += 7;
  const char *title_end = find_bytes(title_begin, end, "</title>", 8);
  if (!title_end || (size_t)(title_end - title_begin) >= title_cap)
    return -1;
  size_t title_length = (size_t)(title_end - title_begin);
  memcpy(title, title_begin, title_length);
  title[title_length] = '\0';

  size_t found_links = 0;
  size_t total_path_bytes = 0;
  uint32_t hash = 2166136261U;
  for (const char *p = title_begin; p < title_end; ++p) {
    hash ^= (unsigned char)*p;
    hash *= 16777619U;
  }

  const char *cursor = title_end + 8;
  while ((cursor = find_bytes(cursor, end, "href=\"", 6))) {
    const char *value = cursor + 6;
    const char *value_end = memchr(value, '"', (size_t)(end - value));
    if (!value_end)
      return -1;
    size_t path_length = href_path_bytes(value, value_end);
    if (path_length != 0) {
      if (total_path_bytes > SIZE_MAX - path_length)
        return -1;
      ++found_links;
      total_path_bytes += path_length;
      for (const char *p = value; p < value_end; ++p) {
        hash = (hash << 5) | (hash >> 27);
        hash ^= (unsigned char)*p;
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
  if (argc != 3 || strncmp(argv[1], "https://", 8) != 0) {
    fprintf(stderr, "usage: %s https://URL CA_CERT\n", argv[0]);
    return 2;
  }
  if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK)
    return 1;

  int result = 1;
  CURL *curl = curl_easy_init();
  struct response body = {0};
  if (!curl)
    goto done;
  if (curl_easy_setopt(curl, CURLOPT_URL, argv[1]) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_CAINFO, argv[2]) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_USERAGENT,
                       "LLVM-OBFUSCATION-HTTP-CLIENT-MARKER") != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "https") != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS_STR, "https") != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 5L) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 5L) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_TIMEOUT, 15L) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, receive_body) != CURLE_OK ||
      curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body) != CURLE_OK)
    goto done;

  CURLcode transfer = curl_easy_perform(curl);
  long status = 0;
  if (transfer != CURLE_OK ||
      curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status) != CURLE_OK ||
      status != 200 || !body.data)
    goto done;

  char title[128];
  size_t links = 0;
  size_t path_bytes = 0;
  uint32_t checksum = 0;
  if (transform_target(body.data, body.length, title, sizeof(title), &links,
                       &path_bytes, &checksum) != 0 ||
      checksum == 0)
    goto done;
  printf("title=%s;links=%zu;path_bytes=%zu\n", title, links, path_bytes);
  result = 0;

done:
  if (result != 0)
    fputs("HTTPS fetch or HTML parse failed\n", stderr);
  free(body.data);
  if (curl)
    curl_easy_cleanup(curl);
  curl_global_cleanup();
  return result;
}
