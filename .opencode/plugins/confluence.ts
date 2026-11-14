import { Plugin } from "@opencode/plugin"
import { readFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"
import { maskPII, maskStoragePII } from "../lib/confluence-pii"

type Settings = { url: string; email: string; token: string }

const SECRETS_FILE = join(homedir(), ".secrets", "confluence.json")

// Читаем креды в момент вызова, а не при загрузке плагина:
// так токен можно положить в файл без перезапуска сервиса.
async function resolveSettings(): Promise<Settings> {
  let file: Partial<Settings> = {}
  try {
    file = JSON.parse(await readFile(SECRETS_FILE, "utf8")) as Partial<Settings>
    if (!file || typeof file !== "object" || Array.isArray(file)
      || [file.url, file.email, file.token].some(value => value !== undefined && typeof value !== "string")) {
      throw new Error("Некорректный формат настроек")
    }
  } catch (error) {
    const code = (error as { code?: string }).code
    if (code !== "ENOENT") {
      // Сообщение JSON.parse может содержать фрагмент файла с credentials.
      throw new Error(`Не удалось прочитать ${SECRETS_FILE}. Проверьте доступ и JSON-формат настроек.`)
    }
  }

  return {
    url: (process.env.CONFLUENCE_URL ?? file.url ?? "").replace(/\/+$/, ""),
    email: process.env.CONFLUENCE_EMAIL ?? file.email ?? "",
    token: process.env.CONFLUENCE_API_TOKEN ?? file.token ?? "",
  }
}

export default Plugin.define({
  id: "confluence",
  async setup(ctx) {
    await ctx.tool.transform((editor) => {
      editor.add({
        name: "confluence_page",
        description:
          "Получить страницу Confluence по pageId (REST API v2, body-format=storage). " +
          "Ответ: pageId, title, version, piiCheck и requirements (storage HTML с PII-маскированием). " +
          `Креды берутся из переменных CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN ` +
          `или из файла ${SECRETS_FILE}.`,
        input: {
          type: "object",
          properties: {
            pageId: { type: "string", pattern: "^[1-9][0-9]*$", description: "ID страницы Confluence" },
          },
          required: ["pageId"],
          additionalProperties: false,
        },
        async execute(input, context) {
          const pageId = (input as { pageId?: unknown } | null | undefined)?.pageId
          if (typeof pageId !== "string" || !/^[1-9][0-9]*$/.test(pageId)) {
            throw new Error("pageId должен быть положительным числовым ID страницы Confluence")
          }
          const settings = await resolveSettings()

          const missing = [
            ["CONFLUENCE_URL", settings.url],
            ["CONFLUENCE_EMAIL", settings.email],
            ["CONFLUENCE_API_TOKEN", settings.token],
          ]
            .filter(([, value]) => !value)
            .map(([name]) => name)

          if (missing.length > 0) {
            throw new Error(
              `Не заданы: ${missing.join(", ")}. ` +
                `Заполните ${SECRETS_FILE} или задайте переменные окружения.`,
            )
          }

          const auth = Buffer.from(`${settings.email}:${settings.token}`).toString("base64")
          const url = `${settings.url}/wiki/api/v2/pages/${encodeURIComponent(pageId)}?body-format=storage`

          const response = await fetch(url, {
            method: "GET",
            headers: {
              Authorization: `Basic ${auth}`,
              Accept: "application/json",
            },
            signal: context.signal,
          })

          if (!response.ok) {
            throw new Error(`Confluence API error: ${response.status}`)
          }

          let data
          try {
            data = await response.json()
          } catch (error) {
            if (context.signal.aborted) throw error
            throw new Error("Ответ Confluence не является корректным JSON")
          }
          const requirements = data?.body?.storage?.value
          if (typeof requirements !== "string") {
            throw new Error("Не удалось получить содержимое страницы Confluence")
          }
          if (String(data.id) !== pageId || typeof data.title !== "string") {
            throw new Error("Ответ Confluence не подтверждает ID и заголовок запрошенной страницы")
          }
          if (!Number.isSafeInteger(data.version?.number) || data.version.number <= 0) {
            throw new Error("Ответ Confluence не содержит фактическую версию страницы")
          }
          const createdAt = data.version.createdAt
          if (createdAt !== undefined && (typeof createdAt !== "string"
            || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(createdAt)
            || !Number.isFinite(Date.parse(createdAt)))) {
            throw new Error("Ответ Confluence содержит некорректную дату версии")
          }
          const piiResult = maskStoragePII(requirements)
          const titleResult = maskPII(data.title)
          const detectedPII = [...new Set([...titleResult.detectedPII, ...piiResult.detectedPII])]
          const result = {
            pageId,
            title: titleResult.maskedText,
            // Номер версии API, не «Версия: 1.0» из текста требований.
            // Автор и произвольное сообщение версии не нужны для resume и могут содержать PII.
            version: {
              number: data.version.number,
              createdAt,
            },
            piiCheck: {
              piiFound: detectedPII.length > 0,
              detectedPII,
            },
            requirements: piiResult.maskedText,
          }
          return { content: JSON.stringify(result, null, 2) }
        },
      })
    })
  },
})
