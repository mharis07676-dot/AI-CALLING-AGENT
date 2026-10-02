/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/**/*.{js,ts,jsx,tsx}", "./components/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0f1c17",
        moss: "#1f3d32",
        leaf: "#3d7a5f",
        sand: "#e8efe9",
        mist: "#f4f7f5",
        signal: "#c45c26",
      },
      fontFamily: {
        display: ["\"Fraunces\"", "Georgia", "serif"],
        body: ["\"Source Sans 3\"", "Segoe UI", "sans-serif"],
      },
    },
  },
  plugins: [],
};
