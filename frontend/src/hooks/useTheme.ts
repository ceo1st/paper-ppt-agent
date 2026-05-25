import { useEffect, useMemo, useState } from "react";

export type Theme = "light" | "dark";
export type ThemePreference = "system" | Theme;

const STORAGE_KEY = "paper-ppt-agent-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

function readThemePreference(): ThemePreference {
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return stored === "light" || stored === "dark" || stored === "system" ? stored : "system";
}

function readSystemTheme(): Theme {
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference);
  const [systemTheme, setSystemTheme] = useState<Theme>(readSystemTheme);
  const theme = useMemo<Theme>(
    () => (preference === "system" ? systemTheme : preference),
    [preference, systemTheme],
  );

  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY);
    const updateSystemTheme = (event: MediaQueryListEvent | MediaQueryList) => {
      setSystemTheme(event.matches ? "dark" : "light");
    };
    updateSystemTheme(media);
    media.addEventListener("change", updateSystemTheme);
    return () => media.removeEventListener("change", updateSystemTheme);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePreference = preference;
    window.localStorage.setItem(STORAGE_KEY, preference);
  }, [preference, theme]);

  return {
    theme,
    preference,
    cycleTheme: () => {
      setPreference((current) => (
        current === "system" ? "light" : current === "light" ? "dark" : "system"
      ));
    },
  };
}
