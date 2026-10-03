using System;
using Npgsql;
using StackExchange.Redis;

namespace Worker
{
    public class Program
    {
        public static int Main(string[] args)
        {
            var pgsql = new NpgsqlConnection("Server=db;Username=postgres;Password=postgres;");
            var redis = ConnectionMultiplexer.Connect("redis");
            return 0;
        }
    }
}
