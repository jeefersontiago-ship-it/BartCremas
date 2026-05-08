import app from "./app";
import { logger } from "./lib/logger";
import { spawn } from "child_process";
import path from "path";

const rawPort = process.env["PORT"];

if (!rawPort) {
  throw new Error(
    "PORT environment variable is required but was not provided.",
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

app.listen(port, (err) => {
  if (err) {
    logger.error({ err }, "Error listening on port");
    process.exit(1);
  }

  logger.info({ port }, "Server listening");

  // Start the Telegram bot as a subprocess
  const botPath = path.resolve(process.cwd(), "bot/bot.py");
  const bot = spawn("python", [botPath], {
    stdio: "inherit",
    cwd: process.cwd(),
  });

  bot.on("error", (err) => {
    logger.error({ err }, "Failed to start Telegram bot");
  });

  bot.on("exit", (code, signal) => {
    logger.warn({ code, signal }, "Telegram bot exited — restarting in 5s");
    setTimeout(() => {
      const retry = spawn("python", [botPath], {
        stdio: "inherit",
        cwd: process.cwd(),
      });
      retry.on("error", (e) => logger.error({ err: e }, "Bot restart failed"));
    }, 5000);
  });

  logger.info({ botPath }, "Telegram bot started");
});
