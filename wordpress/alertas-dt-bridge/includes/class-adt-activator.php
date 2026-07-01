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
     * la última carga. Necesario porque register_activation_hook NO se dispara
     * al actualizar archivos de un plugin ya activo, solo en su primera activación.
     * dbDelta() es seguro de re-ejecutar: agrega columnas/índices faltantes sin
     * eliminar datos ni columnas existentes.
     */
    public static function maybe_upgrade(): void {
        $installed = get_option( 'adt_plugin_version' );
        if ( $installed === ADT_VERSION ) {
            return;
        }

        ADT_Database::create_table();
        update_option( 'adt_plugin_version', ADT_VERSION );
    }
}
