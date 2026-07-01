<?php
defined( 'ABSPATH' ) || exit;

class ADT_Activator {

    public static function activate(): void {
        ADT_Database::create_table();

        if ( ! get_option( 'adt_api_token' ) ) {
            update_option( 'adt_api_token', ADT_Settings::generate_token(), false );
        }

        update_option( 'adt_plugin_version', ADT_VERSION );
        flush_rewrite_rules();
    }

    public static function deactivate(): void {
        flush_rewrite_rules();
    }

    /**
     * Repite la creación/actualización de tabla cuando ADT_VERSION cambió desde
     * la última carga o cuando faltan columnas requeridas en la tabla real.
     * Necesario porque register_activation_hook NO se dispara al actualizar
     * archivos de un plugin ya activo, solo en su primera activación.
     * dbDelta() es seguro de re-ejecutar: agrega columnas/índices faltantes sin
     * eliminar datos ni columnas existentes.
     */
    public static function maybe_upgrade(): void {
        $version_ok = ( get_option( 'adt_plugin_version' ) === ADT_VERSION );
        $schema_ok  = self::schema_is_current();

        if ( $version_ok && $schema_ok ) {
            return;
        }

        ADT_Database::create_table();
        update_option( 'adt_plugin_version', ADT_VERSION );
    }

    /**
     * Verifica que todas las columnas requeridas existen en la tabla real.
     * Cubre el caso donde la versión coincide pero la migración no corrió
     * (ej. adt_plugin_version se escribió antes de que dbDelta se ejecutara).
     */
    private static function schema_is_current(): bool {
        global $wpdb;
        $table = ADT_Database::get_table();

        // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
        $cols = $wpdb->get_col( "SHOW COLUMNS FROM {$table}" );
        if ( empty( $cols ) ) {
            return false; // tabla no existe aún
        }

        $required = [ 'email', 'status', 'subscriber_name', 'phone', 'whatsapp_consent' ];
        foreach ( $required as $col ) {
            if ( ! in_array( $col, $cols, true ) ) {
                return false;
            }
        }
        return true;
    }
}
