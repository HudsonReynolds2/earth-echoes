// ESLint flat config (task E0.4). The no-explicit-any rule enforces the
// handbook section 3 convention: no bare `any` without an explanatory
// eslint-disable comment beside it.
import eslint from "@eslint/js";
import prettier from "eslint-config-prettier";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/", "node_modules/", "playwright-report/", "test-results/"] },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  prettier,
);
