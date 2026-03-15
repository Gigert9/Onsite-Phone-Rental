/*
Run in SSMS as a sysadmin (or equivalent) on the target SQL Server.

1) Optionally change DB name below.
2) Executes: create database, login, user, and tables.

Note: “set up the connection” is done in the app via env vars (see .env.example).
*/

DECLARE @DbName sysname = N'Onsite_Leads_Phone_Dropoff';

IF DB_ID(@DbName) IS NULL
BEGIN
    DECLARE @sql nvarchar(max) = N'CREATE DATABASE [' + REPLACE(@DbName, ']', ']]') + N']';
    EXEC sp_executesql @sql;
END
GO

/*
Create server login.
If your org uses password policies, keep CHECK_POLICY=ON.
*/
IF NOT EXISTS (SELECT 1 FROM sys.sql_logins WHERE name = N'phonerental')
BEGIN
    CREATE LOGIN [phonerental]
        WITH PASSWORD = 'Bra5ura+Onsite26!',
             CHECK_POLICY = ON,
             CHECK_EXPIRATION = OFF;
END
GO

DECLARE @DbName2 sysname = N'Onsite_Leads_Phone_Dropoff';
DECLARE @useSql nvarchar(max) = N'USE [' + REPLACE(@DbName2, ']', ']]') + N'];';
EXEC sp_executesql @useSql;
GO

IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'phonerental')
BEGIN
    CREATE USER [phonerental] FOR LOGIN [phonerental];
END
GO

EXEC sp_addrolemember N'db_datareader', N'phonerental';
EXEC sp_addrolemember N'db_datawriter', N'phonerental';
GO

/*
Tables
*/

IF OBJECT_ID(N'dbo.events', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.events (
        event_id int IDENTITY(1,1) NOT NULL CONSTRAINT PK_events PRIMARY KEY,
        name nvarchar(200) NOT NULL,
        created_at datetime2(0) NOT NULL CONSTRAINT DF_events_created_at DEFAULT (sysutcdatetime()),
        password_salt varbinary(16) NULL,
        password_hash varbinary(32) NULL,
        password_iterations int NULL
    );
END
GO

IF OBJECT_ID(N'dbo.events', N'U') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.events', 'password_salt') IS NULL
        ALTER TABLE dbo.events ADD password_salt varbinary(16) NULL;

    IF COL_LENGTH('dbo.events', 'password_hash') IS NULL
        ALTER TABLE dbo.events ADD password_hash varbinary(32) NULL;

    IF COL_LENGTH('dbo.events', 'password_iterations') IS NULL
        ALTER TABLE dbo.events ADD password_iterations int NULL;
END
GO

IF OBJECT_ID(N'dbo.exhibitors', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.exhibitors (
        exhibitor_id int IDENTITY(1,1) NOT NULL CONSTRAINT PK_exhibitors PRIMARY KEY,
        display_name nvarchar(300) NOT NULL,
        name nvarchar(255) NOT NULL,
        booth nvarchar(50) NULL,
        created_at datetime2(0) NOT NULL CONSTRAINT DF_exhibitors_created_at DEFAULT (sysutcdatetime())
    );

    CREATE INDEX IX_exhibitors_name_booth ON dbo.exhibitors(name, booth);
END
GO

IF OBJECT_ID(N'dbo.event_exhibitors', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.event_exhibitors (
        event_exhibitor_id int IDENTITY(1,1) NOT NULL CONSTRAINT PK_event_exhibitors PRIMARY KEY,
        event_id int NOT NULL,
        exhibitor_id int NOT NULL,

        reserved_phones int NOT NULL,
        reserved_licenses int NULL,

        dropoff_confirmed_phones int NULL,
        dropoff_at datetime2(0) NULL,
        dropoff_printed_name nvarchar(200) NULL,
        dropoff_signature varbinary(max) NULL,
        dropoff_note nvarchar(1000) NULL,

        pickup_confirmed_phones int NULL,
        pickup_at datetime2(0) NULL,
        pickup_printed_name nvarchar(200) NULL,
        pickup_signature varbinary(max) NULL,
        pickup_note nvarchar(1000) NULL,

        CONSTRAINT FK_event_exhibitors_event FOREIGN KEY (event_id) REFERENCES dbo.events(event_id),
        CONSTRAINT FK_event_exhibitors_exhibitor FOREIGN KEY (exhibitor_id) REFERENCES dbo.exhibitors(exhibitor_id),
        CONSTRAINT UQ_event_exhibitors_event_exhibitor UNIQUE (event_id, exhibitor_id)
    );

    CREATE INDEX IX_event_exhibitors_event ON dbo.event_exhibitors(event_id);
END
GO

IF OBJECT_ID(N'dbo.event_exhibitor_actions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.event_exhibitor_actions (
        action_id int IDENTITY(1,1) NOT NULL CONSTRAINT PK_event_exhibitor_actions PRIMARY KEY,
        event_exhibitor_id int NOT NULL,
        action_type nvarchar(10) NOT NULL, -- 'dropoff' | 'pickup'
        quantity int NOT NULL,
        action_at datetime2(0) NOT NULL CONSTRAINT DF_event_exhibitor_actions_action_at DEFAULT (sysutcdatetime()),
        printed_name nvarchar(200) NULL,
        signature varbinary(max) NULL,
        phone_ids nvarchar(max) NULL,
        charger_qty int NULL,
        note nvarchar(1000) NULL,

        CONSTRAINT FK_event_exhibitor_actions_event_exhibitor
            FOREIGN KEY (event_exhibitor_id) REFERENCES dbo.event_exhibitors(event_exhibitor_id)
    );

    CREATE INDEX IX_event_exhibitor_actions_event_exhibitor
        ON dbo.event_exhibitor_actions(event_exhibitor_id, action_at);
END
GO

/*
Schema upgrades (safe ALTERs)
*/

IF OBJECT_ID(N'dbo.event_exhibitors', N'U') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.event_exhibitors', 'dropoff_note') IS NULL
        ALTER TABLE dbo.event_exhibitors ADD dropoff_note nvarchar(1000) NULL;

    IF COL_LENGTH('dbo.event_exhibitors', 'pickup_note') IS NULL
        ALTER TABLE dbo.event_exhibitors ADD pickup_note nvarchar(1000) NULL;

    IF COL_LENGTH('dbo.event_exhibitors', 'dropoff_phone_ids') IS NULL
        ALTER TABLE dbo.event_exhibitors ADD dropoff_phone_ids nvarchar(max) NULL;

    IF COL_LENGTH('dbo.event_exhibitors', 'dropoff_confirmed_chargers') IS NULL
        ALTER TABLE dbo.event_exhibitors ADD dropoff_confirmed_chargers int NULL;

    IF COL_LENGTH('dbo.event_exhibitors', 'pickup_confirmed_chargers') IS NULL
        ALTER TABLE dbo.event_exhibitors ADD pickup_confirmed_chargers int NULL;
END
GO

IF OBJECT_ID(N'dbo.event_exhibitor_actions', N'U') IS NOT NULL
BEGIN
    IF COL_LENGTH('dbo.event_exhibitor_actions', 'phone_ids') IS NULL
        ALTER TABLE dbo.event_exhibitor_actions ADD phone_ids nvarchar(max) NULL;

    IF COL_LENGTH('dbo.event_exhibitor_actions', 'charger_qty') IS NULL
        ALTER TABLE dbo.event_exhibitor_actions ADD charger_qty int NULL;
END
GO
