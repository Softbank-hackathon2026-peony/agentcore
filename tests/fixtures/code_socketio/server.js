const express = require("express");
const http = require("http");
const { Server } = require("socket.io");
const app = express();
const server = http.createServer(app);
const io = new Server(server);
app.get("/health", (req, res) => res.send("ok"));
io.on("connection", (socket) => { socket.on("msg", (m) => io.emit("msg", m)); });
server.listen(process.env.PORT || 3000);
