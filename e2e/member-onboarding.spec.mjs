import { expect, test } from "@playwright/test";

async function responseData(response) {
  return (await response.json()).data;
}

test("an ongoing-project member checkpoints, aligns manually, and completes a forward capture", async ({
  page,
  request,
}) => {
  const projectResponse = await request.post("/projects", {
    data: {
      client_capture_id: "e2e-member-onboarding-project-v1",
      description: "A scientific project already in motion before Lab Tracker adoption.",
      name: "E2E ongoing project orientation",
    },
  });
  expect([200, 201]).toContain(projectResponse.status());
  const project = await responseData(projectResponse);

  await page.goto(`/app/projects/${project.project_id}/onboarding`);
  await expect(
    page.getByRole("heading", { name: /Start tracking E2E ongoing project orientation/ })
  ).toBeVisible();

  await page
    .getByLabel("What output or decision are you working toward now?")
    .fill("Choose the decisive control cohort");
  await page.getByLabel("Question 1").fill("Does the pilot effect persist in controls?");
  await page
    .getByLabel("What recent result or context matters most?")
    .fill("The pilot effect reproduced in two independent sessions");
  await page
    .getByLabel("What is the next move?")
    .fill("Run the matched control comparison");

  const checkpointResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/projects/${project.project_id}/member-onboarding/checkpoint` &&
      response.request().method() === "PUT"
  );
  await page.getByRole("button", { name: "Save tracking checkpoint" }).click();
  expect((await checkpointResponse).ok()).toBe(true);
  await expect(
    page.getByText("Strongest recent context", { exact: true }).locator("..")
  ).toContainText("The pilot effect reproduced in two independent sessions");

  await page.getByRole("button", { name: "Align questions manually" }).click();
  await page.getByLabel("Resolution for question 1").selectOption("checkpoint_only");
  const alignmentResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname ===
        `/projects/${project.project_id}/member-onboarding/manual-alignment` &&
      response.request().method() === "PUT"
  );
  await page.getByRole("button", { name: "Save each resolution" }).click();
  expect((await alignmentResponse).ok()).toBe(true);

  await expect(page.getByRole("heading", { name: "Project-now brief" })).toBeVisible();
  await expect(page.getByLabel("Current-state question map")).toContainText(
    "Does the pilot effect persist in controls?"
  );
  await expect(page.getByLabel("Current-state question map")).toContainText(
    "Checkpoint only"
  );

  await page.getByRole("button", { name: "Make the first capture" }).click();
  await expect(page).toHaveURL(/\/app\/capture\?/);
  await expect(page.getByText("Tracking checkpoint attached", { exact: true })).toBeVisible();
  await expect(
    page.locator('[aria-labelledby="capture-context-title"]').getByRole("combobox").first()
  ).toBeDisabled();
  await page.getByLabel("Message or hint").fill("Control cohort is scheduled for Friday");

  const captureResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/notes" &&
      response.request().method() === "POST"
  );
  await page.getByRole("button", { name: "Save capture" }).click();
  expect((await captureResponse).status()).toBe(201);

  await expect(page).toHaveURL(`/app/projects/${project.project_id}/onboarding`);
  await expect(
    page.locator(".member-onboarding-heading-actions").getByText("Orientation complete", {
      exact: true,
    })
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Finish in workspace" })).toBeEnabled();
});
