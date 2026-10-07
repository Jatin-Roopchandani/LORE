import { act, renderHook, waitFor } from "@testing-library/react";
import single from "./fixtures/single-part.json";
import type { AskEvent } from "./types";

vi.mock("./api", () => ({
  streamAsk: async function* () { for (const e of single as AskEvent[]) yield e; },
}));
Object.defineProperty(window, "matchMedia", { value: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });

test("ask streams to done and lands in history; replay redraws without a request", async () => {
  const { useAsk } = await import("./useAsk");
  const { result } = renderHook(() => useAsk());
  act(() => result.current.ask("How is Jefferson Hope connected to John Ferrier?"));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(result.current.history).toHaveLength(1);
  const id = result.current.history[0].id;
  act(() => result.current.replay(id));
  await waitFor(() => expect(result.current.state.status).toBe("done"));
  expect(result.current.current).toBe(id);
});
