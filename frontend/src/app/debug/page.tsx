"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { FileCode2, Globe2, Layers3, MessagesSquare, Presentation } from "lucide-react";

import { PageEnter } from "@/components/page-enter";
import { PageHeader } from "@/components/page-header";
import { PageLoader } from "@/components/page-loader";
import { Card } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { ChatPanel } from "./components/chat-panel";
import { PptPanel } from "./components/ppt-panel";
import { PsdPanel } from "./components/psd-panel";
import { SearchPanel } from "./components/search-panel";
import { SkillPanel } from "./components/skill-panel";
import { parseDebugTaskLink } from "./task-deep-link";

const tabs = [
  { value: "skills", title: "搜索Skills", icon: FileCode2, description: "复制安装指令，连接你的搜索工具。" },
  { value: "search", title: "搜索", icon: Globe2, description: "提交搜索问题，查看真实回答与来源。" },
  { value: "ppt", title: "PPT生成", icon: Presentation, description: "创建演示任务，查看进度并下载生成文件。" },
  { value: "psd", title: "PSD生成", icon: Layers3, description: "上传参考图片，查看分层文件任务与结果。" },
  { value: "chat", title: "对话", icon: MessagesSquare, description: "组合文字与图片，检查模型的完整返回。" },
];

function DebugPageContent() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  const search = useSearchParams();
  const link = parseDebugTaskLink(search.toString());
  const [activeTab, setActiveTab] = useState(link.tab);
  useEffect(() => { setActiveTab(link.tab); }, [link.tab, link.taskId]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return <PageLoader className="h-full" />;
  }

  return (
    <PageEnter className="flex h-full min-h-0 w-full flex-col gap-6">
      <PageHeader eyebrow="Developer Tools" title="调试" description="直接调用 Skills 搜索、联网搜索、PPT / PSD 生成与对话能力，检查上游返回。" />
      <Tabs value={activeTab} onValueChange={setActiveTab} className="flex min-h-0 w-full flex-1 flex-col gap-4">
        <Card className="shrink-0 gap-3 px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
          <TabsList aria-label="调试能力">
            {tabs.map(({ value, title, icon: Icon }) => (
              <TabsTrigger key={value} value={value}>
                <Icon aria-hidden="true" />
                {title}
              </TabsTrigger>
            ))}
          </TabsList>
          <p className="px-1 text-xs leading-5 text-muted-foreground" aria-live="polite">{tabs.find((tab) => tab.value === activeTab)?.description}</p>
        </Card>
        <TabsContent value="skills" className="min-h-0 overflow-y-auto">
          <SkillPanel />
        </TabsContent>
        <TabsContent value="search" className="min-h-0 overflow-hidden">
          <SearchPanel />
        </TabsContent>
        <TabsContent value="ppt" className="min-h-0 overflow-hidden">
          <PptPanel initialTaskId={link.tab === "ppt" ? link.taskId : null} />
        </TabsContent>
        <TabsContent value="psd" className="min-h-0 overflow-hidden">
          <PsdPanel initialTaskId={link.tab === "psd" ? link.taskId : null} />
        </TabsContent>
        <TabsContent value="chat" className="min-h-0 overflow-hidden">
          <ChatPanel />
        </TabsContent>
      </Tabs>
    </PageEnter>
  );
}

export default function DebugPage() {
  return <Suspense fallback={<PageLoader className="h-full" />}><DebugPageContent /></Suspense>;
}
