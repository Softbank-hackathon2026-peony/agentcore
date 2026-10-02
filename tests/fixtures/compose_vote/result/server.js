var express = require('express'),
    { Pool } = require('pg'),
    app = express(),
    server = require('http').Server(app),
    io = require('socket.io')(server);

var port = process.env.PORT || 4000;

var pool = new Pool({
  connectionString: 'postgres://postgres:postgres@db/postgres'
});

app.get('/', function (req, res) {
  res.send('ok');
});

server.listen(port, function () {
  console.log('App running on port ' + port);
});
