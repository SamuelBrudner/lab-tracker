import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";

// ESLint 10 tracks JSX component references itself. The classic esbuild JSX
// transform still consumes the React namespace implicitly, so model only that
// remaining behavior instead of depending on the legacy React rules plugin.
const classicJsxRuntime = {
  rules: {
    "uses-react": {
      meta: {
        type: "problem",
        schema: [],
      },
      create(context) {
        const markReactAsUsed = (node) => {
          context.sourceCode.markVariableAsUsed("React", node);
        };
        return {
          JSXOpeningElement: markReactAsUsed,
          JSXOpeningFragment: markReactAsUsed,
        };
      },
    },
  },
};

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
      "classic-jsx-runtime": classicJsxRuntime,
      "react-hooks": reactHooks,
    },
    rules: {
      ...js.configs.recommended.rules,
      "classic-jsx-runtime/uses-react": "error",
      "no-console": "warn",
      ...reactHooks.configs.recommended.rules,
      "react-hooks/set-state-in-effect": "off",
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
