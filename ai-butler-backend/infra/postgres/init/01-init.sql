CREATE ROLE butler_migrator LOGIN PASSWORD 'butler_migrator';
CREATE ROLE butler_app LOGIN PASSWORD 'butler_app';
CREATE ROLE butler_test LOGIN PASSWORD 'butler_test';

CREATE DATABASE butler_dev OWNER butler_migrator;
CREATE DATABASE butler_test OWNER butler_migrator;
CREATE DATABASE butler_langgraph_dev OWNER butler_migrator;
CREATE DATABASE butler_langgraph_test OWNER butler_migrator;

GRANT CONNECT ON DATABASE butler_dev TO butler_app;
GRANT CONNECT ON DATABASE butler_test TO butler_test;
GRANT CONNECT ON DATABASE butler_langgraph_dev TO butler_app;
GRANT CONNECT ON DATABASE butler_langgraph_test TO butler_test;

\connect butler_dev
CREATE EXTENSION IF NOT EXISTS vector;

\connect butler_test
CREATE EXTENSION IF NOT EXISTS vector;

\connect butler_langgraph_dev
CREATE EXTENSION IF NOT EXISTS vector;

\connect butler_langgraph_test
CREATE EXTENSION IF NOT EXISTS vector;
