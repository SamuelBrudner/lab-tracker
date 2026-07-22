import { expect, test } from "@playwright/test";

// Flow: share-target HTTP fallback. Playwright's Node-side request fixture sends
// a real multipart POST to /app/share-target and verifies the server's redirect
// contract. This does not emulate an OS share sheet or exercise browser
// navigation; those remain physical-device/manual concerns.
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
