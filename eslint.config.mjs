import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

const testGlobals = {
  ...globals.browser,
  ...globals.node,
  afterEach: "readonly",
  beforeEach: "readonly",
  describe: "readonly",
  expect: "readonly",
  it: "readonly",
  vi: "readonly",
};

export default [
  {
    ignores: [
      "node_modules/**",
      "src/lab_tracker/frontend/**",
    ],
  },
  {
    ...js.configs.recommended,
    files: ["src/lab_tracker/frontend_src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        ...globals.node,
      },
    },
    plugins: {
      react,
      "react-hooks": reactHooks,
    },
    rules: {
      ...js.configs.recommended.rules,
      "no-console": "warn",
      "react/jsx-uses-react": "error",
      "react/jsx-uses-vars": "error",
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    files: [
      "src/lab_tracker/frontend_src/**/*test.{js,jsx}",
      "src/lab_tracker/frontend_src/test/**/*.{js,jsx}",
    ],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: testGlobals,
    },
  },
];
