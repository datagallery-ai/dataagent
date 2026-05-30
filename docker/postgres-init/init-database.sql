-- DataPilot PostgreSQL 数据库初始化脚本
-- 这个脚本会在 PostgreSQL 容器首次启动时执行

-- 创建langgraph数据库
CREATE DATABASE langgraph;

-- 确保用户拥有所有数据库权限
GRANT ALL PRIVILEGES ON DATABASE datapilot TO datapilot_user;
GRANT ALL PRIVILEGES ON DATABASE langgraph TO datapilot_user;

-- 切换到 datapilot 数据库
\c datapilot;

-- 为用户授予模式权限
GRANT ALL ON SCHEMA public TO datapilot_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO datapilot_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO datapilot_user;

-- 设置默认权限，确保新创建的对象也有权限
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO datapilot_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO datapilot_user;

-- 创建一个测试表来验证连接
CREATE TABLE IF NOT EXISTS connection_test (
    id SERIAL PRIMARY KEY,
    message TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 插入测试数据
INSERT INTO connection_test (message) VALUES ('PostgreSQL 容器初始化成功！');

-- 初始化第二个数据库 (langgraph)
\c langgraph;

-- 为用户授予模式权限
GRANT ALL ON SCHEMA public TO datapilot_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO datapilot_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO datapilot_user;

-- 设置默认权限
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO datapilot_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO datapilot_user;

-- 创建 LangGraph 数据库的测试表
CREATE TABLE IF NOT EXISTS langgraph_test (
    id SERIAL PRIMARY KEY,
    graph_name VARCHAR(100) NOT NULL,
    node_count INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 插入测试数据
INSERT INTO langgraph_test (graph_name, node_count) VALUES ('test database langgraph', 5);

-- 显示初始化完成信息
SELECT 'DataPilot PostgreSQL 多数据库初始化完成' AS status;