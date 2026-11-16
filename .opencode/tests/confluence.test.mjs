import assert from "node:assert/strict"
import test from "node:test"
import { page, pluginHarness } from "./test-support.mjs"

const input = { pageId: "6160386" }
const executionContext = () => ({ signal: new AbortController().signal })

test("registers the V2 tool without reading credentials or performing HTTP", async () => {
  const harness = await pluginHarness()
  assert.equal(harness.plugin.id, "confluence")
  assert.equal(harness.tool.name, "confluence_page")
  assert.equal(harness.tool.input.properties.pageId.type, "string")
  assert.deepEqual(harness.reads, [])
  assert.deepEqual(harness.requests, [])
})

test("returns the V2 content envelope and factual version with a minimal projection", async () => {
  const harness = await pluginHarness({ data: page({
    version: { number: 17, createdAt: "2026-11-14T10:20:30.000Z", authorId: "synthetic@example.test", message: "token: SYNTHETIC_VALUE" },
    arbitrary: { password: "SYNTHETIC_VALUE" },
  }) })
  const context = executionContext()
  const output = await harness.tool.execute(input, context)
  assert.deepEqual(Object.keys(output), ["content"])
  assert.equal(typeof output.content, "string")
  assert.deepEqual(JSON.parse(output.content), {
    pageId: "6160386", title: "Synthetic requirements",
    version: { number: 17, createdAt: "2026-11-14T10:20:30.000Z" },
    piiCheck: { piiFound: false, detectedPII: [] },
    requirements: "<p>AUTH-001: example requirement.</p>",
  })
  assert.equal(harness.requests[0].url, "https://example.atlassian.net/wiki/api/v2/pages/6160386?body-format=storage")
  assert.equal(harness.requests[0].init.method, "GET")
  assert.equal(harness.requests[0].init.signal, context.signal)
  assert.equal(harness.requests[0].init.headers.Accept, "application/json")
  assert.equal(harness.requests[0].init.headers.Authorization,
    `Basic ${Buffer.from("synthetic@example.test:SYNTHETIC_API_TOKEN").toString("base64")}`)
  assert.ok(!output.content.includes("SYNTHETIC_VALUE"))
})

test("masks the title and storage, merging and deduplicating PII categories", async () => {
  const harness = await pluginHarness({ data: page({
    title: "synthetic@example.test",
    body: { storage: { value: '<p>synthetic@example.test</p><p>Пароль: <b>synthetic phrase</b></p><p>access_token: SYNTHETIC_VALUE</p><p>+1 202-555-0107</p>' } },
  }) })
  const result = JSON.parse((await harness.tool.execute(input, executionContext())).content)
  assert.equal(result.title, "[EMAIL_MASKED]")
  assert.equal(result.piiCheck.piiFound, true)
  assert.deepEqual([...result.piiCheck.detectedPII].sort(), ["EMAIL", "PASSWORD", "PHONE", "TOKEN"])
  assert.equal(result.requirements, '<p>[EMAIL_MASKED]</p><p>Пароль: <b>[PASSWORD_MASKED]</b></p><p>access_token: [TOKEN_MASKED]</p><p>[PHONE_MASKED]</p>')
})

test("PII found only in the title is still reported", async () => {
  const harness = await pluginHarness({ data: page({ title: "API Key: SYNTHETIC_VALUE" }) })
  const result = JSON.parse((await harness.tool.execute(input, executionContext())).content)
  assert.equal(result.title, "API Key: [TOKEN_MASKED]")
  assert.deepEqual(result.piiCheck, { piiFound: true, detectedPII: ["TOKEN"] })
})

test("reads fallback settings at execution time and honours environment precedence", async () => {
  const harness = await pluginHarness({
    env: { CONFLUENCE_URL: "https://override.example.test///" },
    readFile: async () => JSON.stringify({ url: "https://fallback.example.test", email: "fallback@example.test", token: "SYNTHETIC_FALLBACK_TOKEN" }),
  })
  await harness.tool.execute(input, executionContext())
  assert.equal(harness.reads.length, 1)
  assert.equal(harness.reads[0].encoding, "utf8")
  assert.ok(harness.requests[0].url.startsWith("https://override.example.test/wiki/"))
  assert.equal(harness.requests[0].init.headers.Authorization,
    `Basic ${Buffer.from("fallback@example.test:SYNTHETIC_FALLBACK_TOKEN").toString("base64")}`)
})

test("the version number is returned even if createdAt is absent", async () => {
  const harness = await pluginHarness({ data: page({ version: { number: 9 } }) })
  assert.deepEqual(JSON.parse((await harness.tool.execute(input, executionContext())).content).version, { number: 9 })
})

test("version.createdAt cannot relay arbitrary strings or objects", async () => {
  for (const createdAt of ["password: SYNTHETIC_VALUE", { token: "SYNTHETIC_VALUE" }]) {
    const harness = await pluginHarness({ data: page({ version: { number: 9, createdAt } }) })
    await assert.rejects(harness.tool.execute(input, executionContext()), /Confluence/)
    assert.equal(harness.requests.length, 1)
  }
})

for (const invalid of [undefined, null, {}, { pageId: 6160386 }, { pageId: "0" }, { pageId: "-1" }, { pageId: "01" }, { pageId: "1/../../other" }, { pageId: "6160386\n" }]) {
  test(`rejects invalid input before I/O: ${JSON.stringify(invalid)}`, async () => {
    const harness = await pluginHarness()
    await assert.rejects(harness.tool.execute(invalid, executionContext()), /pageId/)
    assert.equal(harness.reads.length, 0)
    assert.equal(harness.requests.length, 0)
  })
}

test("missing settings block HTTP and report variable names only", async () => {
  const harness = await pluginHarness({ env: {} })
  await assert.rejects(harness.tool.execute(input, executionContext()), /CONFLUENCE_URL.*CONFLUENCE_EMAIL.*CONFLUENCE_API_TOKEN/)
  assert.equal(harness.requests.length, 0)
})

test("corrupt settings JSON is not echoed, because it may contain a credential", async () => {
  const harness = await pluginHarness({ env: {}, readFile: async () => '{"token":"SYNTHETIC_PRIVATE_VALUE", bad}' })
  await assert.rejects(harness.tool.execute(input, executionContext()), (error) => {
    assert.ok(!error.message.includes("SYNTHETIC_PRIVATE_VALUE"))
    assert.match(error.message, /Не удалось прочитать/)
    return true
  })
  assert.equal(harness.requests.length, 0)
})

test("non-ENOENT file errors are not retried or exposed verbatim", async () => {
  const harness = await pluginHarness({ readFile: async () => {
    throw Object.assign(new Error("SYNTHETIC_PRIVATE_VALUE"), { code: "EACCES" })
  } })
  await assert.rejects(harness.tool.execute(input, executionContext()), (error) => !error.message.includes("SYNTHETIC_PRIVATE_VALUE"))
  assert.equal(harness.reads.length, 1)
  assert.equal(harness.requests.length, 0)
})

test("invalid settings shapes fail before HTTP", async () => {
  for (const settings of [null, [], { url: 123 }, { email: {} }, { token: false }]) {
    const harness = await pluginHarness({ env: {}, readFile: async () => JSON.stringify(settings) })
    await assert.rejects(harness.tool.execute(input, executionContext()), /Не удалось прочитать/)
    assert.equal(harness.requests.length, 0)
  }
})

test("HTTP errors do not parse or echo a possibly sensitive response body", async () => {
  const harness = await pluginHarness({ fetch: async () => ({
    ok: false, status: 403, statusText: "Forbidden",
    json: async () => { assert.fail("HTTP error body must not be parsed") },
  }) })
  await assert.rejects(harness.tool.execute(input, executionContext()), /Confluence API error: 403/)
  assert.equal(harness.requests.length, 1)
})

test("HTTP status text is not allowed to relay raw server content", async () => {
  const harness = await pluginHarness({ fetch: async () => ({ ok: false, status: 500, statusText: "SYNTHETIC_PRIVATE_VALUE" }) })
  await assert.rejects(harness.tool.execute(input, executionContext()), (error) => {
    assert.equal(error.message, "Confluence API error: 500")
    return true
  })
})

test("invalid response JSON is not echoed", async () => {
  const harness = await pluginHarness({ fetch: async () => ({
    ok: true, json: async () => { throw new SyntaxError('Unexpected token: "SYNTHETIC_PRIVATE_VALUE"') },
  }) })
  await assert.rejects(harness.tool.execute(input, executionContext()), (error) => {
    assert.ok(!error.message.includes("SYNTHETIC_PRIVATE_VALUE"))
    assert.match(error.message, /Confluence/)
    return true
  })
})

for (const [name, data, message] of [
  ["null response", null, /Confluence/],
  ["missing storage", page({ body: {} }), /содержимое/],
  ["non-string storage", page({ body: { storage: { value: {} } } }), /содержимое/],
  ["wrong page", page({ id: "3964929" }), /ID/],
  ["missing title", page({ title: undefined }), /заголовок/],
  ["missing version", page({ version: undefined }), /версию/],
  ["string version", page({ version: { number: "17" } }), /версию/],
  ["zero version", page({ version: { number: 0 } }), /версию/],
  ["fractional version", page({ version: { number: 1.5 } }), /версию/],
  ["unsafe version", page({ version: { number: Number.MAX_SAFE_INTEGER + 1 } }), /версию/],
]) {
  test(`rejects malformed Confluence data: ${name}`, async () => {
    const harness = await pluginHarness({ fetch: async () => ({ ok: true, json: async () => data }) })
    await assert.rejects(harness.tool.execute(input, executionContext()), message)
    assert.equal(harness.requests.length, 1)
  })
}

test("passes abort signals through and does not retry cancelled requests", async () => {
  const context = executionContext()
  const cancelled = new DOMException("Synthetic cancellation", "AbortError")
  const harness = await pluginHarness({ fetch: async (_url, init) => {
    assert.equal(init.signal, context.signal)
    throw cancelled
  } })
  await assert.rejects(harness.tool.execute(input, context), (error) => error === cancelled)
  assert.equal(harness.requests.length, 1)
})
