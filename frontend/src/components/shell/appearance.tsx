"use client";

import { MotionConfig } from "motion/react";
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { Monitor, SlidersHorizontal, Sparkles } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

type MotionLevel = "standard" | "light" | "off";
type Appearance = {
  motion: MotionLevel;
  transparency: boolean;
  compact: boolean;
};
const defaults: Appearance = {
  motion: "standard",
  transparency: true,
  compact: false,
};
const AppearanceContext = createContext({
  value: defaults,
  reduced: false,
  set: (_value: Partial<Appearance>) => {},
});

export function AppearanceProvider({ children }: { children: ReactNode }) {
  const [value, setValue] = useState<Appearance>(defaults);
  const [systemReduced, setSystemReduced] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  useEffect(() => {
    try {
      const saved = JSON.parse(
        localStorage.getItem("chatgpt2api-appearance") || "{}",
      );
      setValue({
        motion: ["standard", "light", "off"].includes(saved.motion)
          ? saved.motion
          : "standard",
        transparency: saved.transparency !== false,
        compact: saved.compact === true,
      });
    } catch {
      /* 无效偏好仅回落默认，不影响业务页面。 */
    }
    const media = matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setSystemReduced(media.matches);
    const key = () => setKeyboard(true),
      pointer = () => setKeyboard(false);
    update();
    media.addEventListener("change", update);
    document.addEventListener("keydown", key, true);
    document.addEventListener("pointerdown", pointer, true);
    return () => {
      media.removeEventListener("change", update);
      document.removeEventListener("keydown", key, true);
      document.removeEventListener("pointerdown", pointer, true);
    };
  }, []);
  useEffect(() => {
    const root = document.documentElement;
    root.dataset.motion = value.motion;
    root.dataset.transparency = value.transparency ? "standard" : "reduced";
    root.dataset.density = value.compact ? "compact" : "comfortable";
    root.dataset.input = keyboard ? "keyboard" : "pointer";
  }, [value, keyboard]);
  const set = (next: Partial<Appearance>) =>
    setValue((current) => {
      const updated = { ...current, ...next };
      try {
        localStorage.setItem("chatgpt2api-appearance", JSON.stringify(updated));
      } catch {}
      return updated;
    });
  const reduced = systemReduced || value.motion !== "standard" || keyboard;
  return (
    <AppearanceContext.Provider value={{ value, reduced, set }}>
      <MotionConfig
        reducedMotion={reduced ? "always" : "never"}
        transition={{ duration: value.motion === "off" ? 0 : 0.18 }}
      >
        {children}
      </MotionConfig>
    </AppearanceContext.Provider>
  );
}

export const useAppearance = () => useContext(AppearanceContext);

export function AppearanceControl({
  allowGallery = false,
}: {
  allowGallery?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const { value, set } = useAppearance();
  return (
    <>
      <button
        type="button"
        className="glass-icon-button"
        aria-label="动态与外观设置"
        title="动态与外观设置"
        onClick={() => setOpen(true)}
      >
        <SlidersHorizontal size={18} />
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="appearance-dialog">
          <DialogHeader>
            <div className="glass-dialog-symbol">
              <Sparkles size={24} />
            </div>
            <DialogTitle>动态与外观</DialogTitle>
            <DialogDescription>
              让工作台符合你的使用习惯。偏好仅保存在当前浏览器。
            </DialogDescription>
          </DialogHeader>
          <fieldset className="appearance-section">
            <legend>动态效果</legend>
            <div className="appearance-options">
              {(["standard", "light", "off"] as MotionLevel[]).map(
                (mode, i) => (
                  <label
                    key={mode}
                    className={value.motion === mode ? "selected" : ""}
                  >
                    <input
                      type="radio"
                      name="motion-level"
                      checked={value.motion === mode}
                      aria-label={["标准动态", "轻量动态", "关闭动态"][i]}
                      onChange={() => set({ motion: mode })}
                    />
                    <strong>{["标准", "轻量", "关闭"][i]}</strong>
                    <span>
                      {
                        [
                          "连贯、轻盈的操作反馈",
                          "保留状态，减少位移",
                          "即时呈现所有状态",
                        ][i]
                      }
                    </span>
                  </label>
                ),
              )}
            </div>
            <p className="glass-helper">
              系统开启“减少动态效果”时，工作台会自动减少运动。
            </p>
          </fieldset>
          <label className="appearance-toggle">
            <span>
              <strong>玻璃透明效果</strong>
              <small>关闭后使用实色面板，保持清晰层次</small>
            </span>
            <input
              type="checkbox"
              checked={value.transparency}
              onChange={(e) => set({ transparency: e.target.checked })}
            />
          </label>
          <label className="appearance-toggle">
            <span>
              <strong>紧凑列表</strong>
              <small>缩减表格留白，适合高密度数据</small>
            </span>
            <input
              type="checkbox"
              checked={value.compact}
              onChange={(e) => set({ compact: e.target.checked })}
            />
          </label>
          {allowGallery ? (
            <Button
              variant="outline"
              onClick={() => {
                setOpen(false);
                window.dispatchEvent(
                  new Event("chatgpt2api:component-preview"),
                );
              }}
            >
              查看 48 项动态组件
            </Button>
          ) : null}
          <div className="flex items-center justify-between gap-4 border-t pt-4">
            <span className="glass-helper inline-flex items-center gap-2">
              <Monitor size={15} />
              六个板块共享此设置
            </span>
            <Button onClick={() => setOpen(false)}>完成</Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
