import assert from "node:assert/strict"
import test from "node:test"
import { pii } from "./test-support.mjs"

const { maskPII, maskStoragePII } = pii

const textCases = [
  ["Russian password", "пароль: синтетическая фраза из слов", "пароль: [PASSWORD_MASKED]", "PASSWORD"],
  ["uppercase Russian", "ПАРОЛЬ = SYNTHETIC_VALUE", "ПАРОЛЬ = [PASSWORD_MASKED]", "PASSWORD"],
  ["spaced password", "password: several synthetic words", "password: [PASSWORD_MASKED]", "PASSWORD"],
  ["ampersand in password", "password: synthetic & value", "password: [PASSWORD_MASKED]", "PASSWORD"],
  ["quoted password", 'password: "several synthetic words"', 'password: "[PASSWORD_MASKED]"', "PASSWORD"],
  ["single quoted password", "passwd='synthetic phrase'", "passwd='[PASSWORD_MASKED]'", "PASSWORD"],
  ["backtick password", "password: `synthetic phrase`", "password: `[PASSWORD_MASKED]`", "PASSWORD"],
  ["unquoted punctuation", "password: synthetic,phrase;with}punctuation]", "password: [PASSWORD_MASKED]", "PASSWORD"],
  ["marker prefix in password", "password: [PASSWORD_MASKED]SYNTHETIC_VALUE", "password: [PASSWORD_MASKED]", "PASSWORD"],
  ["API Key", "API Key: SYNTHETIC_VALUE", "API Key: [TOKEN_MASKED]", "TOKEN"],
  ["api_key", "api_key=SYNTHETIC_VALUE", "api_key=[TOKEN_MASKED]", "TOKEN"],
  ["api-key", "api-key: SYNTHETIC_VALUE", "api-key: [TOKEN_MASKED]", "TOKEN"],
  ["access token", "access_token: SYNTHETIC_VALUE", "access_token: [TOKEN_MASKED]", "TOKEN"],
  ["refresh token", "refresh-token: SYNTHETIC_VALUE", "refresh-token: [TOKEN_MASKED]", "TOKEN"],
  ["client secret", "client secret: SYNTHETIC_VALUE", "client secret: [TOKEN_MASKED]", "TOKEN"],
  ["Russian token", "токен: SYNTHETIC_VALUE", "токен: [TOKEN_MASKED]", "TOKEN"],
  ["authorization", "Authorization: Bearer SYNTHETIC_VALUE", "Authorization: [TOKEN_MASKED]", "TOKEN"],
  ["email", "Contact: synthetic+qa@example.test.", "Contact: [EMAIL_MASKED].", "EMAIL"],
  ["Unicode email", "тест@пример.рф", "[EMAIL_MASKED]", "EMAIL"],
  ["named entities", "synthetic&commat;example&period;test", "[EMAIL_MASKED]", "EMAIL"],
  ["numeric entities", "password&#58;&#32;&#x1f512;synthetic", "password&#58;&#32;[PASSWORD_MASKED]", "PASSWORD"],
  ["nonbreaking spaces", "API&nbsp;Key:&nbsp;SYNTHETIC_VALUE", "API&nbsp;Key:&nbsp;[TOKEN_MASKED]", "TOKEN"],
  ["international phone", "Call +1 202-555-0107.", "Call [PHONE_MASKED].", "PHONE"],
  ["Russian phone", "+7 (999) 123-45-67", "[PHONE_MASKED]", "PHONE"],
  ["domestic parentheses", "8 (999) 123-45-67", "[PHONE_MASKED]", "PHONE"],
  ["parenthesized phone", "(202) 555-0107", "[PHONE_MASKED]", "PHONE"],
  ["US phone", "202-555-0107", "[PHONE_MASKED]", "PHONE"],
  ["labelled digits", "Телефон: 79991234567", "Телефон: [PHONE_MASKED]", "PHONE"],
  ["JSON phone", '{"phone": "2025550107"}', '{"phone": "[PHONE_MASKED]"}', "PHONE"],
]

for (const [name, input, expected, type] of textCases) {
  test(`text: ${name}`, () => {
    assert.deepEqual(maskPII(input), { piiFound: true, detectedPII: [type], maskedText: expected })
    assert.equal(maskPII(expected).maskedText, expected, "masking must be idempotent")
    assert.equal(maskPII(expected).piiFound, false, "markers are not new PII")
  })
}

test("JSON values preserve syntax and mask escaped quotes and backslashes", () => {
  const input = JSON.stringify({ password: 'synthetic "quoted" phrase\\suffix', access_token: "SYNTHETIC_VALUE", id: 6160386 })
  const actual = maskPII(input)
  assert.deepEqual(JSON.parse(actual.maskedText), { password: "[PASSWORD_MASKED]", access_token: "[TOKEN_MASKED]", id: 6160386 })
  assert.deepEqual(actual.detectedPII, ["PASSWORD", "TOKEN"])
})

test("adjacent assignments do not expose either value", () => {
  assert.equal(maskPII("password: synthetic phrase; API Key: SYNTHETIC_VALUE").maskedText,
    "password: [PASSWORD_MASKED]; API Key: [TOKEN_MASKED]")
  assert.equal(maskPII("password: synthetic phrase token: SYNTHETIC_VALUE").maskedText,
    "password: [PASSWORD_MASKED] token: [TOKEN_MASKED]")
})

test("URL query masking preserves non-sensitive parameters", () => {
  assert.equal(maskPII("https://example.test/?access_token=SYNTHETIC_VALUE&lang=ru").maskedText,
    "https://example.test/?access_token=[TOKEN_MASKED]&lang=ru")
})

test("an empty assignment does not consume the next line", () => {
  const input = "пароль:\nAUTH-001: version 1.0, pageId 6160386"
  assert.deepEqual(maskPII(input), { piiFound: false, detectedPII: [], maskedText: input })
})

for (const input of [
  "pageId: 6160386", "ID: 12345678901", "2026-11-14 10:20:30", "14.11.2026", "12345678.90",
  "range: 1000000000-2000000000", "AUTH-001…AUTH-040", "192.168.100.100", "Версия: 1.0",
  "newpassword: example", "суперпароль: пример", "some_token: example", "&unknown; &#x110000; &#xD800;",
]) {
  test(`preserves non-PII: ${input}`, () => {
    assert.deepEqual(maskPII(input), { piiFound: false, detectedPII: [], maskedText: input })
  })
}

const storageCases = [
  ["inline password", "<p>Пароль: <strong>synthetic</strong> phrase</p>", "<p>Пароль: <strong>[PASSWORD_MASKED]</strong></p>", ["PASSWORD"]],
  ["split email", "<p>synthetic<strong>@example</strong>.test</p>", "<p>[EMAIL_MASKED]<strong></strong></p>", ["EMAIL"]],
  ["entities and quotes", "<p>&quot;password&quot;: &quot;synthetic phrase&quot;</p>", "<p>&quot;password&quot;: &quot;[PASSWORD_MASKED]&quot;</p>", ["PASSWORD"]],
  ["attribute password", '<div data-password="synthetic phrase">OK</div>', '<div data-password="[PASSWORD_MASKED]">OK</div>', ["PASSWORD"]],
  ["attribute token", "<div data-api-key='SYNTHETIC_VALUE'>OK</div>", "<div data-api-key='[TOKEN_MASKED]'>OK</div>", ["TOKEN"]],
  ["unquoted attribute", "<div token=SYNTHETIC_VALUE>OK</div>", "<div token=[TOKEN_MASKED]>OK</div>", ["TOKEN"]],
  ["self-closing unquoted attribute", "<input token=SYNTHETIC_VALUE/>", "<input token=[TOKEN_MASKED]/>", ["TOKEN"]],
  ["Unicode attribute", '<span пароль="synthetic phrase">OK</span>', '<span пароль="[PASSWORD_MASKED]">OK</span>', ["PASSWORD"]],
  ["attribute phone", '<span data-phone="2025550107">OK</span>', '<span data-phone="[PHONE_MASKED]">OK</span>', ["PHONE"]],
  ["mailto", '<a href="mailto:synthetic@example.test">Contact</a>', '<a href="mailto:[EMAIL_MASKED]">Contact</a>', ["EMAIL"]],
  ["attribute query", '<a href="https://example.test/?token=SYNTHETIC_VALUE&amp;lang=ru">Docs</a>', '<a href="https://example.test/?token=[TOKEN_MASKED]&amp;lang=ru">Docs</a>', ["TOKEN"]],
  ["greater-than inside attribute", '<span title="password: synthetic > phrase">OK</span>', '<span title="password: [PASSWORD_MASKED]">OK</span>', ["PASSWORD"]],
  ["CDATA", '<ac:plain-text-body><![CDATA[{"access_token":"SYNTHETIC_VALUE"}]]></ac:plain-text-body>', '<ac:plain-text-body><![CDATA[{"access_token":"[TOKEN_MASKED]"}]]></ac:plain-text-body>', ["TOKEN"]],
  ["comment", "<!-- пароль: synthetic phrase --><p>OK</p>", "<!-- пароль: [PASSWORD_MASKED] --><p>OK</p>", ["PASSWORD"]],
  ["marker suffix", '<span data-token="[TOKEN_MASKED]SYNTHETIC_VALUE">OK</span>', '<span data-token="[TOKEN_MASKED]">OK</span>', ["TOKEN"]],
]

for (const [name, input, expected, types] of storageCases) {
  test(`storage: ${name}`, () => {
    assert.deepEqual(maskStoragePII(input), { piiFound: true, detectedPII: types, maskedText: expected })
    assert.deepEqual(maskStoragePII(expected), { piiFound: false, detectedPII: [], maskedText: expected })
  })
}

test("all categories are detected once without damaging surrounding XHTML", () => {
  const input = '<div data-password="SYNTHETIC_VALUE"><p>API Key: SYNTHETIC_VALUE</p><p>synthetic@example.test; +1 202-555-0107</p></div>'
  const actual = maskStoragePII(input)
  assert.deepEqual(actual.detectedPII, ["PASSWORD", "TOKEN", "EMAIL", "PHONE"])
  assert.equal(actual.maskedText, '<div data-password="[PASSWORD_MASKED]"><p>API Key: [TOKEN_MASKED]</p><p>[EMAIL_MASKED]; [PHONE_MASKED]</p></div>')
})

test("storage without PII retains links, dates, numeric IDs and entities byte-for-byte", () => {
  const input = '<p ac:local-id="12345678901">2026-11-14: AUTH-001…AUTH-040</p><a href="https://example.test/pages/6160386?count=10&amp;lang=ru">Docs&nbsp;1.0</a>'
  assert.deepEqual(maskStoragePII(input), { piiFound: false, detectedPII: [], maskedText: input })
})

test("empty HTML password field does not redact the following requirement", () => {
  const input = "<p>Пароль:</p><p>AUTH-001: ordinary requirement</p>"
  assert.equal(maskStoragePII(input).maskedText, input)
})

test("CDATA entities are literal text, not XHTML escapes", () => {
  const input = "<ac:plain-text-body><![CDATA[password: synthetic &quot;phrase&quot;]]></ac:plain-text-body>"
  assert.equal(maskStoragePII(input).maskedText,
    "<ac:plain-text-body><![CDATA[password: [PASSWORD_MASKED]]]></ac:plain-text-body>")
})

test("plain CDATA text and escapes remain unchanged without PII", () => {
  const input = "<ac:plain-text-body><![CDATA[&quot; &amp; <not-a-tag> pageId=6160386]]></ac:plain-text-body>"
  assert.deepEqual(maskStoragePII(input), { piiFound: false, detectedPII: [], maskedText: input })
})
