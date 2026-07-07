import { jsonError, readArtifactForRole } from "../../../_conversationStore";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

type RouteContext = { params: Promise<{ artifactId: string }> };

export async function GET(_request: Request, context: RouteContext) {
  const { artifactId } = await context.params;
  try {
    const artifact = await readArtifactForRole(
      artifactId,
      process.env.WAJE_GATEWAY_ROLE || "analyst",
      "export",
    );
    return new Response(markdownForArtifact(artifact), {
      headers: {
        "content-type": "text/markdown; charset=utf-8",
        "content-disposition": `attachment; filename="${artifactId.replace(/[^a-zA-Z0-9._-]+/g, "-")}.md"`,
      },
    });
  } catch (error) {
    return jsonError(error);
  }
}

function markdownForArtifact(artifact: Awaited<ReturnType<typeof readArtifactForRole>>) {
  const summary = sectionPayload(artifact.answerPackage, "summary");
  const title = String(summary.final_business_summary ? "业务分析结果" : artifact.id);
  const answer = String(summary.final_business_summary || summary.answer_text || "");
  const notice = artifact.hiddenSectionCount > 0 ? "\n\n> 部分细分结果因权限不可见。\n" : "";
  return [`# ${title}`, "", answer || "当前权限下没有可导出的业务摘要。", notice].join("\n").trim() + "\n";
}

function sectionPayload(answerPackage: Record<string, unknown>, sectionId: string) {
  const sections = Array.isArray(answerPackage.sections) ? answerPackage.sections : [];
  const section = sections.find((item) => {
    if (!item || typeof item !== "object") return false;
    return (item as Record<string, unknown>).section_id === sectionId;
  });
  if (!section || typeof section !== "object") return {};
  const payload = (section as Record<string, unknown>).payload;
  return payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
}
