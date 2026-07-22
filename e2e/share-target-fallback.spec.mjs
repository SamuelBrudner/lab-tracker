import { expect, test } from "@playwright/test";

// Flow: OS share-sheet fallback. The service worker's share-target handler POSTs
// the shared file to /app/share-target; when it can't be handled inline the
// server must 303 the browser to the capture screen (never 200/500). jsdom
// cannot exercise a real multipart POST + redirect, so this runs in-browser.
test("share-target POST falls back to a 303 redirect to capture", async ({ request }) => {
  const response = await request.post("/app/share-target", {
    multipart: {
      file: {
        name: "shared.jpg",
        mimeType: "image/jpeg",
        buffer: Buffer.from("fake-image-bytes"),
      },
    },
    maxRedirects: 0,
  });

  expect(response.status()).toBe(303);
  expect(response.headers()["location"]).toBe("/app/capture");
});
