"use client";
import { useEffect, useRef, useState } from "react";
import { motion } from "motion/react";
import {
  Check,
  ChevronRight,
  Copy,
  ImageIcon,
  Layers3,
  LoaderCircle,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { useAppearance } from "@/components/shell/appearance";

const groups = [
  {
    id: "A",
    title: "导航与框架",
    names: [
      "玻璃侧栏",
      "选中底板",
      "导航图标反馈",
      "滚动边缘层",
      "页面切换",
      "命令入口",
      "任务胶囊",
      "主题控制",
    ],
  },
  {
    id: "B",
    title: "输入与选择",
    names: [
      "触感按钮",
      "图标按钮",
      "搜索输入",
      "分段控件",
      "模型选择",
      "状态开关",
      "数值控制",
      "筛选标签",
    ],
  },
  {
    id: "C",
    title: "浮层与操作",
    names: [
      "上下文提示",
      "弹出菜单",
      "右侧检视器",
      "底部面板",
      "确认弹窗",
      "批量操作条",
      "反馈通知",
      "分步导入",
    ],
  },
  {
    id: "D",
    title: "列表与数据",
    names: [
      "统计卡片",
      "状态标签",
      "行选择",
      "列表重排",
      "行内详情",
      "分页反馈",
      "加载骨架",
      "空状态与错误",
    ],
  },
  {
    id: "E",
    title: "图片与操控",
    names: [
      "文件拖入",
      "参考图胶片",
      "任务卡片",
      "阶段时间线",
      "图片呈现",
      "图片预览",
      "缩放画布",
      "网格与列表",
    ],
  },
  {
    id: "F",
    title: "任务与反馈",
    names: [
      "请求状态",
      "复制反馈",
      "字段校验",
      "未保存状态",
      "连接测试",
      "继续查询",
      "新记录提示",
      "任务面板",
    ],
  },
];

function Example({
  group,
  index,
  name,
}: {
  group: string;
  index: number;
  name: string;
}) {
  const [on, setOn] = useState(false),
    [value, setValue] = useState(""),
    [step, setStep] = useState(0),
    [busy, setBusy] = useState(false),
    [dialog, setDialog] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { reduced } = useAppearance();
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  const cycle = () => {
    if (timer.current) clearTimeout(timer.current);
    setBusy(true);
    timer.current = setTimeout(() => {
      setBusy(false);
      setStep((current) => current + 1);
    }, 650);
  };
  let content;
  if (group === "B") {
    content =
      index === 0 ? (
        <Button onClick={() => setOn(!on)}>
          {on ? <Check size={15} /> : <Sparkles size={15} />}{" "}
          {on ? "已选择" : "试试按下"}
        </Button>
      ) : index === 1 ? (
        <Button
          size="icon"
          aria-label="切换收藏状态"
          variant={on ? "default" : "outline"}
          onClick={() => setOn(!on)}
        >
          {on ? <Check /> : <Sparkles />}
        </Button>
      ) : index === 2 ? (
        <Input
          aria-label="示例搜索"
          placeholder="搜索模型或页面…"
          value={value}
          maxLength={128}
          onChange={(event) => setValue(event.target.value)}
        />
      ) : index === 3 ? (
        <div className="demo-segment">
          {["画布", "历史"].map((label, i) => (
            <button
              key={label}
              data-active={step === i}
              onClick={() => setStep(i)}
            >
              {label}
            </button>
          ))}
        </div>
      ) : index === 4 ? (
        <select
          aria-label="示例模型"
          className="demo-select"
          value={value}
          onChange={(event) => setValue(event.target.value)}
        >
          <option value="">选择模型</option>
          <option>gpt-image-2</option>
          <option>gpt-image-2.5</option>
        </select>
      ) : index === 5 ? (
        <label className="flex items-center gap-3">
          <Switch checked={on} onCheckedChange={setOn} />
          <span>{on ? "已开启" : "已关闭"}</span>
        </label>
      ) : index === 6 ? (
        <label className="w-full">
          <input
            aria-label="示例数值"
            type="range"
            min="1"
            max="8"
            value={step + 1}
            onChange={(event) => setStep(Number(event.target.value) - 1)}
            className="w-full accent-primary"
          />
          <span>{step + 1} 张</span>
        </label>
      ) : (
        <div className="flex flex-wrap gap-2">
          {["全部", "正常", "限流"].map((label, i) => (
            <Button
              key={label}
              size="sm"
              variant={step === i ? "tinted" : "ghost"}
              onClick={() => setStep(i)}
            >
              {label}
            </Button>
          ))}
        </div>
      );
  } else if (group === "C") {
    content =
      index === 0 ? (
        <button
          className="demo-tip"
          onFocus={() => setOn(true)}
          onBlur={() => setOn(false)}
          onMouseEnter={() => setOn(true)}
          onMouseLeave={() => setOn(false)}
        >
          悬停或聚焦
          {on ? <span role="tooltip">所有操作都有清楚说明</span> : null}
        </button>
      ) : index === 1 ? (
        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline">打开菜单</Button>
          </PopoverTrigger>
          <PopoverContent className="w-40">
            <button className="demo-menu-item" onClick={() => setOn(!on)}>
              <Copy size={14} />
              {on ? "已选择" : "复制信息"}
            </button>
            <button className="demo-menu-item" onClick={() => setDialog(true)}>
              <Layers3 size={14} />
              查看详情
            </button>
          </PopoverContent>
        </Popover>
      ) : [2, 3, 4].includes(index) ? (
        <Button variant="outline" onClick={() => setDialog(true)}>
          {index === 4 ? "确认操作" : "打开面板"}
          <ChevronRight size={14} />
        </Button>
      ) : index === 5 ? (
        <div className="w-full">
          <Button size="sm" variant="outline" onClick={() => setOn(!on)}>
            {on ? "取消选择" : "选择 3 项"}
          </Button>
          {on ? (
            <motion.div
              initial={reduced ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              className="demo-batch"
            >
              已选 3 项 <button onClick={() => setOn(false)}>清空</button>
            </motion.div>
          ) : null}
        </div>
      ) : index === 6 ? (
        <div>
          <Button size="sm" variant="outline" onClick={() => setOn(!on)}>
            显示反馈
          </Button>
          {on ? (
            <div role="status" className="demo-notice">
              <Check size={14} />
              示例操作已完成
            </div>
          ) : null}
        </div>
      ) : (
        <div className="w-full">
          <div className="demo-steps">
            {["准备", "预览", "完成"].map((label, i) => (
              <span key={label} data-active={step % 3 >= i}>
                {label}
              </span>
            ))}
          </div>
          <Button size="sm" variant="outline" onClick={() => setStep(step + 1)}>
            下一步
          </Button>
        </div>
      );
  } else if (group === "D") {
    content =
      index === 0 ? (
        <div className="flex w-full items-center justify-between">
          <strong className="text-3xl font-semibold tabular-nums">
            {24 + step}
          </strong>
          <Button size="sm" variant="outline" onClick={() => setStep(step + 1)}>
            更新
          </Button>
        </div>
      ) : index === 1 ? (
        <div className="flex items-center gap-3">
          <Badge variant={on ? "success" : "warning"}>
            {on ? "正常" : "待确认"}
          </Badge>
          <Button size="sm" variant="ghost" onClick={() => setOn(!on)}>
            切换
          </Button>
        </div>
      ) : index === 2 ? (
        <label className={`demo-row ${on ? "selected" : ""}`}>
          <input
            type="checkbox"
            checked={on}
            onChange={(event) => setOn(event.target.checked)}
          />
          示例账号 {on ? "· 已选" : ""}
        </label>
      ) : index === 3 ? (
        <div className="w-full">
          <Button size="sm" variant="ghost" onClick={() => setOn(!on)}>
            反转排序
          </Button>
          {(on ? [3, 2, 1] : [1, 2, 3]).map((number) => (
            <motion.div
              layout={reduced ? false : "position"}
              key={number}
              className="demo-row"
            >
              记录 {number}
            </motion.div>
          ))}
        </div>
      ) : index === 4 ? (
        <div className="w-full">
          <Button variant="ghost" size="sm" onClick={() => setOn(!on)}>
            {on ? "收起" : "展开"}详情
          </Button>
          {on ? (
            <p className="demo-notice">次级信息按需出现，主列表保持清楚。</p>
          ) : null}
        </div>
      ) : index === 5 ? (
        <div className="demo-segment">
          {[1, 2, 3].map((number) => (
            <button
              key={number}
              data-active={step % 3 === number - 1}
              onClick={() => setStep(number - 1)}
            >
              {number}
            </button>
          ))}
        </div>
      ) : index === 6 ? (
        <div className="w-full">
          <Button variant="ghost" size="sm" onClick={cycle}>
            重新加载
          </Button>
          {busy ? (
            <div className="demo-skeleton" />
          ) : (
            <div className="demo-row">
              <Check size={14} />
              内容已就绪
            </div>
          )}
        </div>
      ) : (
        <div className="text-center">
          <ImageIcon className="mx-auto mb-2 text-muted-foreground" />
          <span>{on ? "暂时无法连接" : "没有匹配的记录"}</span>
          <Button variant="ghost" size="sm" onClick={() => setOn(!on)}>
            切换状态
          </Button>
        </div>
      );
  } else if (group === "E") {
    content =
      index === 0 ? (
        <label
          className="demo-dropzone"
          onDragOver={(event) => {
            event.preventDefault();
            setOn(true);
          }}
          onDragLeave={() => setOn(false)}
          onDrop={(event) => {
            event.preventDefault();
            setOn(false);
            setValue(`${event.dataTransfer.files.length} 个文件`);
          }}
          data-active={on}
        >
          <ImageIcon size={20} />
          {value || "拖入或选择示例文件"}
          <input
            type="file"
            accept="image/*"
            className="sr-only"
            onChange={(event) =>
              setValue(event.target.files?.[0]?.name.slice(0, 80) || "")
            }
          />
        </label>
      ) : index === 1 ? (
        <div className="flex items-center gap-2">
          {Array.from({ length: Math.min(3, step + 1) }, (_, i) => (
            <span className="demo-photo small" key={i} />
          ))}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setStep((step + 1) % 3)}
          >
            添加
          </Button>
        </div>
      ) : index === 2 ? (
        <div className="demo-row w-full">
          {busy ? (
            <LoaderCircle className="animate-spin" size={18} />
          ) : (
            <Check size={18} />
          )}
          <span>{busy ? "处理中" : "示例任务已就绪"}</span>
          <Button size="sm" variant="ghost" disabled={busy} onClick={cycle}>
            演示
          </Button>
        </div>
      ) : index === 3 ? (
        <div className="w-full">
          <div className="demo-steps">
            {["排队", "处理", "结果"].map((label, i) => (
              <span key={label} data-active={step % 3 >= i}>
                {label}
              </span>
            ))}
          </div>
          <Button size="sm" variant="ghost" onClick={() => setStep(step + 1)}>
            推进阶段
          </Button>
        </div>
      ) : index === 4 ? (
        <button
          className="demo-photo"
          aria-label="切换图片呈现"
          style={{ opacity: on ? 0.45 : 1 }}
          onClick={() => setOn(!on)}
        />
      ) : index === 5 ? (
        <button
          className="demo-photo"
          aria-label="预览示例图片"
          onClick={() => setDialog(true)}
        >
          <ImageIcon size={23} />
        </button>
      ) : index === 6 ? (
        <div className="w-full overflow-hidden">
          <div
            className="demo-photo mx-auto"
            style={{ transform: `scale(${1 + step / 10})` }}
          />
          <input
            aria-label="缩放示例画布"
            className="mt-3 w-full accent-primary"
            type="range"
            min="0"
            max="5"
            value={step}
            onChange={(event) => setStep(Number(event.target.value))}
          />
        </div>
      ) : (
        <div className="w-full">
          <Button size="sm" variant="ghost" onClick={() => setOn(!on)}>
            切换视图
          </Button>
          <div
            className={on ? "flex flex-col gap-1" : "grid grid-cols-3 gap-2"}
          >
            {[1, 2, 3].map((number) => (
              <motion.div
                layout={reduced ? false : "position"}
                className="demo-photo small"
                key={number}
              />
            ))}
          </div>
        </div>
      );
  } else if (group === "F") {
    content =
      index === 0 || index === 4 ? (
        <Button disabled={busy} onClick={cycle}>
          {busy ? (
            <LoaderCircle size={15} className="animate-spin" />
          ) : step ? (
            <Check size={15} />
          ) : (
            <RefreshCw size={15} />
          )}{" "}
          {busy
            ? "演示处理中"
            : step
              ? "演示已完成"
              : index === 4
                ? "测试演示"
                : "提交演示"}
        </Button>
      ) : index === 1 ? (
        <Button
          variant="outline"
          onClick={() =>
            void navigator.clipboard
              .writeText("example-value")
              .then(() => setOn(true))
              .catch(() => setValue("复制不可用"))
          }
        >
          {on ? <Check size={15} /> : <Copy size={15} />}{" "}
          {value || (on ? "已复制" : "复制示例")}
        </Button>
      ) : index === 2 ? (
        <div className="w-full">
          <Input
            aria-label="字段校验示例"
            value={value}
            placeholder="请输入至少 3 个字符"
            onChange={(event) => setValue(event.target.value.slice(0, 128))}
          />
          {value.length > 0 && value.length < 3 ? (
            <p role="alert" className="mt-2 text-xs text-destructive">
              请至少输入 3 个字符
            </p>
          ) : null}
        </div>
      ) : index === 3 ? (
        <div className="w-full">
          <Input
            aria-label="草稿示例"
            value={value}
            placeholder="修改后显示未保存提示"
            onChange={(event) => {
              setValue(event.target.value.slice(0, 128));
              setOn(true);
            }}
          />
          {on ? (
            <div className="demo-batch">
              有未保存更改{" "}
              <button onClick={() => setOn(false)}>保存示例</button>
            </div>
          ) : null}
        </div>
      ) : index === 5 ? (
        <div className="text-center">
          <span className="glass-helper">连接暂时中断</span>
          <Button
            variant="outline"
            size="sm"
            className="ml-2"
            disabled={busy}
            onClick={cycle}
          >
            {busy ? "读取中" : step ? "已恢复" : "继续查询"}
          </Button>
        </div>
      ) : index === 6 ? (
        <div className="w-full">
          {!on ? (
            <button className="demo-notice w-full" onClick={() => setOn(true)}>
              有 3 条新记录，点击查看
            </button>
          ) : (
            <div className="demo-row">
              <Check size={15} />
              已定位新记录
            </div>
          )}
        </div>
      ) : (
        <Button variant="outline" onClick={() => setDialog(true)}>
          <Layers3 size={15} />
          查看示例任务
        </Button>
      );
  } else {
    content =
      index === 0 ? (
        <div className="flex items-center gap-3">
          <button
            className="demo-mini-sidebar"
            data-active={on}
            onClick={() => setOn(!on)}
          >
            <Layers3 size={18} />
            {on ? <span>工作台</span> : null}
          </button>
          <span className="glass-helper">点击展开</span>
        </div>
      ) : index === 1 || index === 4 ? (
        <div className="demo-segment">
          {["生图", "号池", "图片"].map((label, i) => (
            <button
              key={label}
              data-active={step % 3 === i}
              onClick={() => setStep(i)}
            >
              {label}
            </button>
          ))}
        </div>
      ) : index === 2 ? (
        <Button
          size="icon"
          variant={on ? "tinted" : "ghost"}
          onClick={() => setOn(!on)}
          aria-label="示例导航反馈"
        >
          <Sparkles />
        </Button>
      ) : index === 3 ? (
        <div className="w-full overflow-hidden rounded-xl border">
          <div className="sticky top-0 bg-background/90 px-3 py-2 text-xs backdrop-blur">
            工具栏
          </div>
          <div className="max-h-16 overflow-y-auto p-3 text-xs text-muted-foreground">
            滚动查看内容
            <br />
            保持清晰的边缘
            <br />
            信息始终可读
            <br />
            玻璃层独立于内容
          </div>
        </div>
      ) : index === 5 ? (
        <Button variant="outline" onClick={() => setDialog(true)}>
          搜索页面 <kbd className="ml-2 text-xs opacity-50">⌘K</kbd>
        </Button>
      ) : index === 6 ? (
        <Button variant="outline" onClick={cycle}>
          {busy ? (
            <LoaderCircle size={15} className="animate-spin" />
          ) : (
            <Layers3 size={15} />
          )}{" "}
          {busy ? "1 个任务" : "任务就绪"}
        </Button>
      ) : (
        <button
          className={`demo-theme ${on ? "dark-demo" : ""}`}
          onClick={() => setOn(!on)}
        >
          {on ? "深色层次" : "浅色层次"}
        </button>
      );
  }
  return (
    <div
      data-component-example={`${group}${String(index + 1).padStart(2, "0")}`}
      className="component-demo-card"
    >
      <div className="component-demo-heading">
        <code>
          {group}
          {String(index + 1).padStart(2, "0")}
        </code>
        <h2>{name}</h2>
      </div>
      <div className="component-demo-stage">{content}</div>
      <Dialog open={dialog} onOpenChange={setDialog}>
        <DialogContent
          className={
            group === "C" && index === 2
              ? "demo-inspector"
              : group === "C" && index === 3
                ? "demo-sheet"
                : undefined
          }
        >
          <DialogTitle>{name}</DialogTitle>
          <DialogDescription>
            这是独立的组件演示，不会提交业务操作。
          </DialogDescription>
          <div className="demo-photo mx-auto" />
          <Button onClick={() => setDialog(false)}>完成</Button>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function ComponentDemoPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  if (isCheckingAuth || session?.role !== "admin") return null;
  return (
    <section className="flex flex-col gap-5">
      <PageHeader
        eyebrow="INTERACTION LIBRARY"
        title="动态组件"
        description="48 项交互模式，六个页面共用一套清晰、轻盈的动态语言。"
      />
      <p className="glass-helper">
        此处使用独立演示数据，可直接操作查看效果，不调用业务接口。
      </p>
      <Tabs defaultValue="A">
        <TabsList>
          {groups.map((group) => (
            <TabsTrigger key={group.id} value={group.id}>
              {group.title}
            </TabsTrigger>
          ))}
        </TabsList>
        {groups.map((group) => (
          <TabsContent key={group.id} value={group.id} keepMounted={false}>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {group.names.map((name, index) => (
                <Example
                  key={name}
                  group={group.id}
                  index={index}
                  name={name}
                />
              ))}
            </div>
          </TabsContent>
        ))}
      </Tabs>
    </section>
  );
}
