const app = require("express")();
app.get("/", (q, s) => s.send("ok"));
app.listen(3000);
