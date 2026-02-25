import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { LongText } from "../App";

describe("LongText", () => {
  it("toggles expand and collapse", async () => {
    const user = userEvent.setup();
    render(<LongText text={"长文本".repeat(80)} lines={2} allowToggle />);
    const expand = screen.getByRole("button", { name: "展开" });
    await user.click(expand);
    expect(screen.getByRole("button", { name: "收起" })).toBeInTheDocument();
  });

  it("hides toggle for short text", () => {
    render(<LongText text="短文本" lines={2} allowToggle />);
    expect(screen.queryByRole("button", { name: "展开" })).toBeNull();
  });
});
