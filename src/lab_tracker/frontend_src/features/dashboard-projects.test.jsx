import * as React from "react";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Dashboard } from "./dashboard-projects.jsx";

function baseProps(overrides = {}) {
  return {
    busy: false,
    canManageProjectMembers: true,
    canWrite: true,
    datasetCount: 0,
    memberRole: "contributor",
    memberUsername: "",
    noteCount: 0,
    onAddProjectMember: vi.fn((event) => event.preventDefault()),
    onCreateProject: vi.fn((event) => event.preventDefault()),
    onMemberRoleChange: vi.fn(),
    onMemberUsernameChange: vi.fn(),
    onProjectDescriptionChange: vi.fn(),
    onProjectNameChange: vi.fn(),
    onRemoveProjectMember: vi.fn(),
    onSelectedProjectChange: vi.fn(),
    onUpdateProjectMember: vi.fn(),
    projectDescription: "",
    projectMembers: [],
    projectName: "",
    projects: [{ name: "Temporal odor coding", project_id: "project-1" }],
    questionCount: 0,
    selectedProjectId: "project-1",
    ...overrides,
  };
}

describe("Dashboard project members", () => {
  it("renders project members and owner controls", () => {
    const onUpdateProjectMember = vi.fn();
    const onRemoveProjectMember = vi.fn();
    render(
      <Dashboard
        {...baseProps({
          onRemoveProjectMember,
          onUpdateProjectMember,
          projectMembers: [
            {
              membership_id: "membership-1",
              role: "contributor",
              user_id: "user-1",
              username: "undergrad",
            },
          ],
        })}
      />
    );

    expect(screen.getByText("Project Members")).toBeInTheDocument();
    expect(screen.getByText("undergrad")).toBeInTheDocument();

    const memberRow = screen.getByText("undergrad").closest("article");
    const memberRoleSelect = memberRow.querySelector("select");
    fireEvent.change(memberRoleSelect, { target: { value: "owner" } });
    expect(onUpdateProjectMember).toHaveBeenCalledWith("user-1", "owner");

    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    expect(onRemoveProjectMember).toHaveBeenCalledWith("user-1");
  });

  it("hides member mutation controls from non-owners", () => {
    render(
      <Dashboard
        {...baseProps({
          canManageProjectMembers: false,
          projectMembers: [
            {
              membership_id: "membership-1",
              role: "viewer",
              user_id: "user-1",
              username: "undergrad",
            },
          ],
        })}
      />
    );

    expect(screen.queryByRole("button", { name: "Add member" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove" })).toBeDisabled();
  });
});
