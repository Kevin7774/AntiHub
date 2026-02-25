import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ErrorBoundary from "../ErrorBoundary";

function Crash() {
  throw new Error("boom");
}

describe("ErrorBoundary", () => {
  it("renders fallback UI when child crashes", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ErrorBoundary>
          <Crash />
        </ErrorBoundary>
      );
      expect(screen.getByText("页面出现异常")).toBeInTheDocument();
      expect(screen.getByText("重新加载")).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});
