from flask_mysqldb import MySQL
import MySQLdb.cursors

mysql = MySQL()

def get_cursor(app_mysql):
    return app_mysql.connection.cursor(MySQLdb.cursors.DictCursor)
